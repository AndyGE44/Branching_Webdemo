# Agent-Safe Toy Inventory

A FastAPI web demo for showing how checkpoint-lite can give a normal web
service an agent-safe branch workflow:

```text
main state -> create checkpoint-lite branch -> agent explores -> diff -> discard/commit
```

The main demo path is the shared Ubuntu VM with checkpoint-lite enabled. The
local-copy backend exists only for quick frontend/API development on non-Linux
machines and does not exercise checkpoint-lite.

## Shared VM Demo With SSH Port Forwarding

This private repo is currently tested on the shared Ubuntu VM reachable as:

```bash
ssh sf-exp
```

Use SSH port forwarding to view the VM-hosted checkpoint-lite demo from your
local browser without opening public inbound ports.

### 1. Open A Tunnel From Your Laptop

Run this on your laptop and keep the terminal open:

```bash
ssh \
  -o ExitOnForwardFailure=yes \
  -L 18000:127.0.0.1:8000 \
  -L 18200:127.0.0.1:8200 \
  -L 18201:127.0.0.1:8201 \
  -L 18202:127.0.0.1:8202 \
  sf-exp
```

Port meanings:

- `18000`: forwards your laptop's `127.0.0.1:18000` to the VM main FastAPI app
  on `127.0.0.1:8000`
- `18200+`: forwards your laptop's `127.0.0.1:18200+` to the VM checkpoint-lite
  branch apps on `127.0.0.1:8200+`

If you create more than three branches at once, add more forwarded ports, for
example `-L 18203:127.0.0.1:8203`.

Using `18000` and `18200+` avoids colliding with a local copy of this demo that
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

### 3. Start The Checkpoint-Lite Demo On The VM

Still inside the SSH session:

```bash
cd ~/Web_Demo_For_Checkpointlite
. .venv/bin/activate

export TOY_BRANCH_BACKEND=checkpoint-lite
export CHECKPOINT_LITE_BIN=/users/alexxjk/checkpoint-lite/checkpoint-lite
export TOY_CHECKPOINT_SESSIONS_DIR=/tmp/checkpoint-sessions
export TOY_BRANCH_HOST=127.0.0.1
export TOY_BRANCH_PORT_START=8200
export TOY_CHECKPOINT_USE_SUDO=1
export PYTHONPATH=src

uvicorn agent_safe_demo.main:app --host 127.0.0.1 --port 8000
```

### 4. Open The UI Locally

On your laptop, open:

```text
http://127.0.0.1:18000
```

Try this flow:

```text
Create Agent Branch -> Run Agent -> Diff -> Open Branch -> Discard
```

The header should show `checkpoint-lite / checkpoint-lite-cli`. The
`Backend & Snapshot Stats` panel shows the active backend, base count, branch
count, visible snapshot-tree nodes, and measured snapshot/restore calls for the
current server process.

The branch URL should look like:

```text
http://127.0.0.1:8200
```

With the safer tunnel above, open the branch locally as:

```text
http://127.0.0.1:18200
```

The app may still display the VM-side branch URL `http://127.0.0.1:8200`.
Manually replace local port `8200` with `18200` in your browser. Branch `8201`
maps to local `18201`, branch `8202` maps to local `18202`, and so on.

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
lsof -tiTCP:8200-8250 -sTCP:LISTEN | xargs -r kill
```

You are seeing the checkpoint-lite VM version when:

- Branch IDs start with `ckpt-`.
- The branch card includes checkpoint-lite session/base details.
- Branch apps use VM ports `8200+`, viewed locally through `18200+`.

You are probably seeing the local development version when:

- Branch IDs start with `br-`.
- Branch apps use local-copy ports around `8100+`.
- The UI does not show checkpoint-lite session/base details.

### 6. Optional Smoke Test On The VM

In another SSH session or after stopping the server:

```bash
cd ~/Web_Demo_For_Checkpointlite
. .venv/bin/activate
python scripts/smoke-test.py
```

Expected result:

```text
build_orders delta: +1
purchase_orders delta: +1
audit_log delta: +3
main state after agent: unchanged
```

### 7. Cleanup

If a run is interrupted, clean up ports and checkpoint-lite mounts:

```bash
lsof -tiTCP:8000 -sTCP:LISTEN | xargs -r kill
lsof -tiTCP:8200-8250 -sTCP:LISTEN | xargs -r sudo kill
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

### 5. Verify OverlayFS With Checkpoint-Lite

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

## Checkpoint-Lite Backend Quick Reference

This is the generic Ubuntu/EC2 quick start, not the preferred shared VM path.
Use it when you are running on a Linux host where you intentionally want the app
to listen on all interfaces. For the shared VM, prefer SSH port forwarding and
`--host 127.0.0.1`.

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

Target lifecycle:

```text
create base   -> checkpoint-lite init -> checkpoint-lite create <base-id>
create branch -> checkpoint-lite restore <base-id>
              -> start branch app URL in a restored layer
run agent     -> HTTP calls against branch URL
              -> create step snapshots after each agent action
status        -> /api/backend reports backend mode and snapshot/restore stats
discard       -> checkpoint-lite cleanup branch state
commit        -> promote branch state to main
reset         -> delete active branches, bases, sessions, and reset main DB
```

## StateFork Backend Quick Reference

This is the experimental adapter for moving from direct checkpoint-lite calls to
StateFork's Python controller API. It uses the same UI and FastAPI endpoints as
the checkpoint-lite backend, but selects `StateForkBackend`:

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
`create_env_from_snapshot`, and `cleanup` methods. During `Run Agent`, it takes
a new StateFork snapshot after each agent action and returns those nodes to the
UI as a small tree under the branch card:

```text
base checkpoint
└── create blocked build order
    └── try substitute part
        └── draft purchase order
```

It is intentionally behind the `TOY_BRANCH_BACKEND=statefork` flag while the
direct checkpoint-lite backend remains the primary shared VM demo path.
The same `Backend & Snapshot Stats` UI and `GET /api/backend` endpoint work in
this mode, with the method shown as `statefork:<method>`.

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

- `GET /api/inventory`
- `POST /api/reservations`
- `POST /api/build-orders`
- `POST /api/build-orders/{id}/try-substitute`
- `POST /api/purchase-orders`
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
- `POST /api/branches/{branch_id}/run-agent-demo`
- `GET /api/branches/{branch_id}/diff`
- `POST /api/branches/{branch_id}/commit`
- `POST /api/branches/{branch_id}/discard`

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
