# EC2 Deployment Plan

This document defines the deployment track for turning the current StateFork
demo into a hosted web service. It is not a final production runbook yet. The
first goal is to prove that EC2 can run the same StateFork/checkpoint-lite
workflow currently validated on the shared Ubuntu VM.

## Goals

- Run the FastAPI demo on EC2 with `TOY_BRANCH_BACKEND=statefork`.
- Validate StateFork snapshot, restore, branch server startup, diff, commit, and
  discard on EC2.
- Provide a path from SSH-tunneled demo access to a public URL.
- Identify what must change before multi-user login is safe.

## Non-Goals For The First EC2 Pass

- No public multi-user login yet.
- No long-running public deployment without HTTPS and access control.
- No assumption that commit is a general StateFork merge. Current commit remains
  application-level SQLite promotion.
- No broad public exposure of branch ports unless explicitly testing public demo
  mode.

## Recommended EC2 Phases

### Phase 1: Private EC2 Compatibility Smoke

Keep the app private and access it through SSH port forwarding, same as the
shared VM. This answers the important infrastructure question first:

```text
Can this EC2 instance run StateFork/checkpoint-lite correctly?
```

Use:

- Ubuntu LTS AMI.
- x86_64 instance first, unless StateFork/checkpoint-lite is already validated
  on ARM.
- At least 2 vCPU and 4 GiB RAM for comfortable testing.
- EBS root volume >= 30 GiB.
- Security group inbound:
  - TCP 22 from your IP only.
- No inbound 8000/8300+ yet.

Access:

```bash
ssh \
  -o ExitOnForwardFailure=yes \
  -L 18000:127.0.0.1:8000 \
  -L 18300:127.0.0.1:8300 \
  -L 18301:127.0.0.1:8301 \
  -L 18302:127.0.0.1:8302 \
  ubuntu@<ec2-public-dns>
```

Open locally:

```text
http://127.0.0.1:18000
```

### Phase 2: Public Demo Mode

Only after Phase 1 passes. Use this when someone else needs to operate the demo
without SSH.

Minimum setup:

- Main app listens on `127.0.0.1:8000`.
- Put Nginx/Caddy in front on ports 80/443.
- Add HTTPS.
- Add simple access control before sharing the URL.
- Route branch app traffic through the reverse proxy rather than exposing a wide
  port range directly.

Avoid this except for a short demo window:

```text
Security group inbound 8000 and 8300-8350 from 0.0.0.0/0
```

If you temporarily do it, restrict source IPs and remove the rules immediately
after the demo.

### Phase 3: Real Web Product Mode

Required before a real website with login:

- Authentication.
- Per-user authorization.
- Per-user branch/session isolation.
- Branch cleanup jobs.
- Persistent metadata database.
- HTTPS and domain.
- Audit logs.
- Rate limits.
- Deployment service manager, likely `systemd`.
- Reverse proxy route model for branch sessions.
- Clear commit semantics per service.

## EC2 Instance Setup Checklist

Install system packages:

```bash
sudo apt-get update
sudo apt-get install -y \
  git \
  curl \
  python3 \
  python3-pip \
  python3.12-venv \
  criu \
  golang-go
```

Validate CRIU:

```bash
sudo criu check
```

Clone this repo:

```bash
git clone git@github.com:AndyGE44/Web_Demo_For_Checkpointlite.git
cd Web_Demo_For_Checkpointlite
```

Prepare Python:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

Prepare checkpoint-lite:

```bash
cd /users/alexxjk
git clone git@github.com:Alex-XJK/checkpoint-lite.git
cd checkpoint-lite
go build -o checkpoint-lite cmd/checkpoint-lite/main.go
go build -o bash_init cmd/bash-init/main.go
./checkpoint-lite version
```

Prepare StateFork:

```bash
cd /users/alexxjk
git clone git@github.com:Alex-XJK/StateFork.git
cd StateFork
```

If `/users/alexxjk` is not appropriate on EC2, use a project-owned path such as
`/opt/statefork-demo` and update all environment variables accordingly.

## Overlay / Checkpoint Smoke

Run this before starting the web app:

```bash
sudo umount -l /tmp/checkpoint-sessions/*/work 2>/dev/null || true
sudo rm -rf /tmp/checkpoint-sessions /tmp/checkpoint-sessions-info

mkdir -p /tmp/ckpt-lite-min
echo hello > /tmp/ckpt-lite-min/hello.txt

sudo env CHECKPOINT_SESSIONS_DIR=/tmp/checkpoint-sessions \
  /users/alexxjk/checkpoint-lite/checkpoint-lite \
  init /tmp/ckpt-lite-min --quiet
```

Expected output:

```text
<session-id>,/tmp/checkpoint-sessions/<session-id>/work
```

Cleanup:

```bash
sudo umount -l /tmp/checkpoint-sessions/*/work 2>/dev/null || true
sudo rm -rf /tmp/checkpoint-sessions /tmp/checkpoint-sessions-info /tmp/ckpt-lite-min
```

## Start StateFork Demo On EC2

Private SSH-tunneled mode:

```bash
cd ~/Web_Demo_For_Checkpointlite
. .venv/bin/activate

export TOY_BRANCH_BACKEND=statefork
export TOY_STATEFORK_ROOT=/users/alexxjk/StateFork
export TOY_STATEFORK_CWD=/users/alexxjk/StateFork
export TOY_STATEFORK_METHOD=ckpt_build
export CHECKPOINT_SESSIONS_DIR=/tmp/checkpoint-sessions
export TOY_BRANCH_HOST=127.0.0.1
export TOY_BRANCH_PORT_START=8300
export PYTHONPATH=src

sudo -E .venv/bin/uvicorn agent_safe_demo.main:app --host 127.0.0.1 --port 8000
```

Expected UI flow:

```text
Create Base -> Create Branch -> Run Agent -> Diff -> Commit or Discard
```

Expected agent snapshots:

```text
Sell 3 CASE-42
Buy 5 SENSOR-9
Reserve 2 MCU-100
```

## Public URL Strategy

Do not expose raw Uvicorn as the final public service. Use a reverse proxy.

Initial public demo option:

```text
Internet -> HTTPS reverse proxy -> 127.0.0.1:8000 main app
```

Branch URL problem:

```text
Branch services currently run on 127.0.0.1:8300+
```

For a real public demo, branch access should become one of:

- Path routing:
  - `/branches/<branch_id>/...` -> branch internal port.
- Subdomain routing:
  - `<branch_id>.demo.example.com` -> branch internal port.
- Main-app proxy:
  - The main FastAPI app proxies branch requests.

Until that exists, public branch access through raw ports is only acceptable for
short, controlled testing.

## Login Architecture Direction

The current app has no user model. Before external users log in:

- Add users table or external auth provider.
- Associate bases and branches with `user_id`.
- Prevent one user from seeing or committing another user's branch.
- Add server-side session management.
- Add cleanup policy for abandoned branch servers.
- Persist branch metadata outside process memory.

Recommended first auth implementation:

```text
FastAPI app + reverse proxy + managed auth provider or simple single-user gate
```

For a research demo, start with a single shared demo password. For a real
multi-user site, use a proper identity provider.

## Known EC2 Risks

- CRIU support can depend on kernel, AMI, instance type, and privileges.
- OverlayFS mount operations require permissions; expect `sudo` requirements.
- Branch process cleanup must be reliable before public use.
- Current commit is SQLite backup, not conflict-aware merge.
- Current branch/base metadata is in process memory.
- Exposing branch ports directly is not a production design.
- Costs can accumulate if instances, EBS volumes, Elastic IPs, or load balancers
  are left running.

## Exit Criteria For EC2 Compatibility

EC2 is acceptable as the next deployment environment only after:

- `pytest -q` passes.
- `sudo criu check` passes or the limitation is understood.
- checkpoint-lite overlay smoke passes.
- StateFork smoke passes with:
  - base creation
  - branch creation
  - `Run Agent`
  - three snapshots
  - non-empty diff
  - reset cleanup
- SSH-tunneled UI works from your laptop.
- Cleanup leaves no branch Uvicorn processes or stale mounts.

