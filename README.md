# Agent-Safe Toy Mailbox

A FastAPI web demo for showing how StateFork and checkpoint-lite can give a
normal email-style web service an agent-safe branch workflow:

```text
main mailbox state -> create StateFork base -> create branch -> discard/commit
```

This branch is Phase 1 of the email server migration. The UI now uses mailbox,
message, label, and draft primitives. The StateFork/checkpoint-lite branch
lifecycle is still intact, while email-specific agent actions and semantic diff
will land in later phases.

The preferred demo path is the shared Ubuntu VM with `StateForkBackend`
enabled. StateFork uses its controller API to call snapshot, restore,
create-env, and cleanup operations. The direct checkpoint-lite backend remains
available as a lower-level reference path, and the local-copy backend exists
only for quick frontend/API development on non-Linux machines.

## Public Cloudflare Quick Tunnel Demo

Use this when you want to send someone a temporary public URL for the VM-hosted
main app. This keeps VM inbound ports closed: `cloudflared` makes an outbound
connection to Cloudflare and proxies the generated `trycloudflare.com` URL back
to `127.0.0.1:8000` on the VM.

Quick Tunnel is for short demos only. The URL changes whenever the tunnel is
restarted, and it does not replace real authentication or a named Cloudflare
Tunnel for longer-running deployments.

### 1. Prepare `.env` On The VM

The real `.env` file is intentionally ignored by git. Create it on the VM:

```bash
ssh sf-exp
cd ~/Web_Demo_For_Checkpointlite
cp .env.example .env
```

Edit `.env` and replace `TOY_DEMO_AUTH_PASSWORD` with a real demo password:

```bash
nano .env
```

Minimum required value:

```bash
TOY_DEMO_AUTH_PASSWORD=<shared-demo-password>
```

`TOY_DEMO_AUTH_USER` defaults to `demo`. The password protects the main app
with HTTP Basic Auth. Branch apps are still internal VM-only processes on
`127.0.0.1:8300+`, so the main app can manage branch environments without
opening those branch ports publicly.

### 2. Install `cloudflared` If Needed

```bash
if ! command -v cloudflared >/dev/null 2>&1; then
  curl -L --fail --show-error --output /tmp/cloudflared.deb \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$(dpkg --print-architecture).deb"
  sudo dpkg -i /tmp/cloudflared.deb
fi
cloudflared --version
```

### 3. Start The Password-Protected Main App

```bash
cd ~/Web_Demo_For_Checkpointlite
git pull

python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest -q

tmux new -d -s agent-main './scripts/run-public-main.sh'
```

Check that unauthenticated requests are blocked and authenticated requests work:

```bash
curl -i http://127.0.0.1:8000/api/backend
curl -u demo:<shared-demo-password> http://127.0.0.1:8000/api/backend
```

### 4. Start The Public Quick Tunnel

```bash
tmux new -d -s cf-main './scripts/run-cloudflare-quick-tunnel.sh'
tmux capture-pane -pt cf-main -S -80
```

Copy the printed `https://...trycloudflare.com` URL and share it with the demo
username and password.

### 5. Stop Public Access

Stop only the public URL:

```bash
tmux kill-session -t cf-main
```

Stop the app too:

```bash
tmux kill-session -t agent-main
```

## Shared VM Demo With SSH Port Forwarding

This private repo is currently tested on the shared Ubuntu VM reachable as:

```bash
ssh sf-exp
```

Use SSH port forwarding to view the VM-hosted StateFork demo from your
local browser without opening public inbound ports.

### 1. Open A Tunnel From Your Laptop

Run this on your laptop and keep the terminal open:

```bash
ssh \
  -o ExitOnForwardFailure=yes \
  -L 18000:127.0.0.1:8000 \
  -L 18300:127.0.0.1:8300 \
  -L 18301:127.0.0.1:8301 \
  -L 18302:127.0.0.1:8302 \
  sf-exp
```

Port meanings:

- `18000`: forwards your laptop's `127.0.0.1:18000` to the VM main FastAPI app
  on `127.0.0.1:8000`
- `18300+`: forwards your laptop's `127.0.0.1:18300+` to the VM StateFork
  branch apps on `127.0.0.1:8300+`

If you create more than three branches at once, add more forwarded ports, for
example `-L 18303:127.0.0.1:8303`.

Using `18000` and `18300+` avoids colliding with a local copy of this demo that
may already be running on your laptop. The `ExitOnForwardFailure=yes` option
makes SSH fail immediately if a requested local port is already occupied, instead
of silently leaving you connected to the wrong server.

### 2. Prepare The Repo On The VM

Inside the SSH session:

```bash
cd ~/Web_Demo_For_Checkpointlite
git pull

python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

If the VM was freshly rebuilt and `venv` support is missing:

```bash
sudo apt-get update
sudo apt-get install -y python3.12-venv
```

### 3. Start The StateFork Demo On The VM

Still inside the SSH session:

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

### 4. Open The UI Locally

On your laptop, open:

```text
http://127.0.0.1:18000
```

Try this flow:

```text
Create Base -> Create Branch -> Open Branch -> Commit or Discard
```

The header should show `statefork / statefork:ckpt_build`. The
`Backend & Snapshot Stats` panel shows the active backend, base count, branch
count, visible snapshot-tree nodes, and measured snapshot/restore calls for the
current server process.

The branch URL should look like:

```text
http://127.0.0.1:8300
```

With the safer tunnel above, open the branch locally as:

```text
http://127.0.0.1:18300
```

The app may still display the VM-side branch URL `http://127.0.0.1:8300`.
Manually replace local port `8300` with `18300` in your browser. Branch `8301`
maps to local `18301`, branch `8302` maps to local `18302`, and so on.

### 5. Avoid Accidentally Opening A Local Demo

If you forwarded ports but still see the local-copy version, your laptop may
already have a local server listening on the same port.

Check whether your laptop has a local main server on `8000`:

```bash
lsof -iTCP:8000 -sTCP:LISTEN -n -P
```

Stop local demo servers before testing the VM:

```bash
lsof -tiTCP:8000 -sTCP:LISTEN | xargs -r kill
lsof -tiTCP:8300-8350 -sTCP:LISTEN | xargs -r kill
```

You are seeing the preferred StateFork VM version when:

- The header shows `statefork / statefork:ckpt_build`.
- Branch IDs start with `sf-`.
- Branch apps use VM ports `8300+`, viewed locally through `18300+`.

You are probably seeing the local development version when:

- Branch IDs start with `br-`.
- Branch apps use local-copy ports around `8100+`.
- The header shows `local-copy / file-copy`.

### 6. Optional Smoke Test On The VM

In another SSH session or after stopping the server:

```bash
cd ~/Web_Demo_For_Checkpointlite
. .venv/bin/activate
python scripts/smoke-test.py
```

This smoke test still covers the legacy inventory agent path that will be
replaced by the email agent flow in a later phase. Expected result:

```text
CASE-42 on_hand delta: -3
SENSOR-9 on_hand delta: +5
MCU-100 reserved delta: +2
audit_log delta: +1
main state after agent run: unchanged
```

### 7. Cleanup

If a run is interrupted, clean up ports and checkpoint-lite/StateFork session
mounts:

```bash
lsof -tiTCP:8000 -sTCP:LISTEN | xargs -r kill
lsof -tiTCP:8300-8350 -sTCP:LISTEN | xargs -r sudo kill
sudo umount -l /tmp/checkpoint-sessions/*/work 2>/dev/null || true
sudo rm -rf /tmp/checkpoint-sessions /tmp/checkpoint-sessions-info
```

## Bootstrap A Fresh Shared VM

Use this section when `sf-exp` points to a newly rebuilt VM with the same OS
configuration as the current shared VM but no project files.

Assumptions:

- You can SSH to the VM with `ssh sf-exp`.
- You have `sudo` on the VM.
- Your GitHub SSH key can access this private repo.
- The StateFork source is available at `/users/alexxjk/StateFork`, or you can
  clone it there.
- The checkpoint-lite source or binary is available at
  `/users/alexxjk/checkpoint-lite`, or you can clone/build it there.

### 1. Install System Packages

On the VM:

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

sudo criu check
```

`sudo criu check` should print success. If it fails, checkpoint-lite process
checkpointing is not ready on that VM.

### 2. Clone This Private Repo

On the VM:

```bash
cd ~
git clone git@github.com:AndyGE44/Web_Demo_For_Checkpointlite.git
cd Web_Demo_For_Checkpointlite
```

If the repo already exists and you want a clean copy:

```bash
cd ~
rm -rf Web_Demo_For_Checkpointlite
git clone git@github.com:AndyGE44/Web_Demo_For_Checkpointlite.git
cd Web_Demo_For_Checkpointlite
```

### 3. Prepare The Python Environment

On the VM:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

### 4. Prepare Checkpoint-Lite

If `/users/alexxjk/checkpoint-lite/checkpoint-lite` already exists:

```bash
/users/alexxjk/checkpoint-lite/checkpoint-lite version
```

If checkpoint-lite is missing, clone/build it on the VM:

```bash
cd /users/alexxjk
git clone git@github.com:Alex-XJK/checkpoint-lite.git
cd checkpoint-lite
go build -o checkpoint-lite cmd/checkpoint-lite/main.go
go build -o bash_init cmd/bash-init/main.go
./checkpoint-lite version
```

### 5. Prepare StateFork

If `/users/alexxjk/StateFork` already exists:

```bash
cd /users/alexxjk/StateFork
git pull --ff-only
```

If StateFork is missing, clone it on the VM:

```bash
cd /users/alexxjk
git clone git@github.com:Alex-XJK/StateFork.git
cd StateFork
```

The shared VM demo expects `TOY_STATEFORK_ROOT` and `TOY_STATEFORK_CWD` to point
at this directory.

### 6. Verify OverlayFS With Checkpoint-Lite

On the VM:

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

If you see `mount command failed: exit status 32`, make sure
`CHECKPOINT_SESSIONS_DIR=/tmp/checkpoint-sessions` is set. Some VM images have
old checkpoint-lite config pointing at `/mydata2/checkpoint-sessions`.

Clean up after the check:

```bash
sudo umount -l /tmp/checkpoint-sessions/*/work 2>/dev/null || true
sudo rm -rf /tmp/checkpoint-sessions /tmp/checkpoint-sessions-info /tmp/ckpt-lite-min
```

Then run the shared VM demo above.

## StateFork Backend Quick Reference

This is the preferred backend for the shared VM demo. It uses StateFork's Python
controller API instead of calling checkpoint-lite directly from the web app. The
UI and FastAPI endpoints stay the same, but the branch backend is selected with
`TOY_BRANCH_BACKEND=statefork`:

```bash
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

`StateForkBackend` currently calls StateFork's `snapshot`, `restore`,
`create_env_from_snapshot`, and `cleanup` methods. The current Phase 1 mailbox
UI shows base checkpoints and branch environments. The legacy inventory agent
endpoint still exists internally while the email agent flow is being migrated.

Target lifecycle:

```text
create base   -> StateFork snapshot
create branch -> StateFork restore <base-id>
              -> StateFork create_env_from_snapshot <base-id>
              -> start branch app URL in the forked environment
run agent     -> legacy inventory endpoint; email agent flow coming later
status        -> /api/backend reports statefork:<method> and snapshot/restore stats
discard       -> terminate branch app and cleanup StateFork environment
commit        -> promote branch state to main
reset         -> delete active branches, bases, sessions, and reset main DB
```

The direct checkpoint-lite backend remains available as a lower-level reference
path. The same `Backend & Snapshot Stats` UI and `GET /api/backend` endpoint
work in this mode, with the method shown as `statefork:<method>`.

## Checkpoint-Lite Backend Quick Reference

This is the generic Ubuntu/EC2 quick start for the lower-level direct
checkpoint-lite backend, not the preferred shared VM path. Use it when you are
running on a Linux host where you intentionally want the app to call
checkpoint-lite directly or listen on all interfaces. For the shared VM, prefer
StateFork with SSH port forwarding and `--host 127.0.0.1`.

```bash
export TOY_BRANCH_BACKEND=checkpoint-lite
export CHECKPOINT_LITE_BIN=/path/to/checkpoint-lite
export TOY_CHECKPOINT_SESSIONS_DIR=/tmp/checkpoint-sessions
export TOY_BRANCH_HOST=0.0.0.0
export TOY_BRANCH_PORT_START=8200
export TOY_CHECKPOINT_USE_SUDO=1
export PYTHONPATH=src

uvicorn agent_safe_demo.main:app --host 0.0.0.0 --port 8000
```

The `CheckpointLiteBackend` is in `src/agent_safe_demo/branching.py`. More VM
setup notes live in `docs/ubuntu-checkpoint-lite.md`.

Direct checkpoint-lite lifecycle:

```text
create base   -> checkpoint-lite init -> checkpoint-lite create <base-id>
create branch -> checkpoint-lite restore <base-id>
              -> start branch app URL in a restored layer
run agent     -> legacy inventory endpoint; email agent flow coming later
status        -> /api/backend reports checkpoint-lite-cli and snapshot/restore stats
discard       -> checkpoint-lite cleanup branch state
commit        -> promote branch state to main
reset         -> delete active branches, bases, sessions, and reset main DB
```

## Repository Layout

```text
agent_safe_demo/
├── src/agent_safe_demo/       # FastAPI app, branch backends, static UI
├── tests/                     # API tests
├── docs/                      # Ubuntu / checkpoint-lite setup notes
├── scripts/                   # Local run and smoke-test helpers
├── pyproject.toml             # Python project metadata and dependencies
├── requirements.txt           # Convenience install entrypoint
└── README.md                  # Project overview
```

Generated runtime data is ignored by git:

```text
toy_inventory.db
.branches/
build/
dist/
```

## Useful Endpoints

- `GET /api/mailbox`
- `GET /api/messages`
- `GET /api/messages/{message_id}`
- `GET /api/state`
- `POST /api/reset` clears active branches, base checkpoints, backend sessions,
  and recreates the main toy database
- `GET /api/backend`
- `GET /api/bases`
- `POST /api/bases`
- `DELETE /api/bases/{base_id}`
- `POST /api/bases/{base_id}/branches`
- `GET /api/branches`
- `POST /api/branches`
- `POST /api/branches/{branch_id}/commit`
- `POST /api/branches/{branch_id}/discard`

Legacy inventory endpoints and the old demo-agent diff endpoint still exist for
compatibility while the email agent implementation is being built.

The generated OpenAPI docs are available at `/docs`.

## Local Development Without Checkpoint-Lite

This mode is only for fast local development on macOS or non-Linux machines.
It does not use checkpoint-lite. Branches are simulated by copying
`toy_inventory.db` into `.branches/<branch_id>/`.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
./scripts/run-dev.sh
```

Open:

```text
http://127.0.0.1:8000
```

Run tests:

```bash
pytest -q
```

For an end-to-end local-copy smoke test while the dev server is running:

```bash
python scripts/smoke-test.py
```
