# Agent-Safe Multi-App Demo

A FastAPI web demo for showing how StateFork can give normal app-plane web
services an agent-safe branch workflow:

```text
open workspace -> initial checkpoint -> user/agent changes -> snapshot/restore
```

The control UI is app-agnostic: it selects an app-plane service, starts that
service in a managed runtime, embeds the runtime UI, and exposes snapshot/restore
controls around it. The StateFork base and branch lifecycle remains underneath
that workspace controller.

The demo is split into two API surfaces:

- `agent_safe_demo.app_plane.email_service.app:app`,
  `agent_safe_demo.app_plane.inventory_service.app:app`, and
  `agent_safe_demo.app_plane.kv_service.app:app` are ordinary business apps.
  They expose their own runtime UI plus app-specific APIs.
- `agent_safe_demo.control_plane.main:app` is the StateFork workspace
  controller. It owns app selection, snapshot, restore, runtime startup, and the
  checkpoint UI.

Runtime branches are launched from the selected app manifest and registry
entry, so the managed program does not know it has been branched. Control
plane and app-plane imports should use the package paths above directly.

The app plane is intentionally directory-based: each child under
`agent_safe_demo/app_plane/` owns one independent app. New apps are primarily
registered with `statefork.yaml`; the Python registry only supplies local demo
adapters such as seed initialization and optional agent-demo actions. The KV
service is launched through a wrapper script to exercise the generic runtime
launcher path.

The repo now exposes a single backend: `StateForkBackend`. StateFork uses its
controller API to call snapshot, restore, create-env, and cleanup operations.

The repo includes a `Dockerfile` for checkpoint-lite/StateFork build mode. The
build image contains Python, the app package, and a shell-capable runtime,
which is the intended path for demonstrating that StateFork can manage an
ordinary packaged web service from the outside.

`StateForkBackend` keeps the current VM-stable init path by default. Set
`DEMO_STATEFORK_BUILD=1` before starting the controller to ask StateFork to use
the Dockerfile build path.

## Shopgym Storefronts (Shopify Hydrogen shops)

The `shop_clothing`, `shop_cookware`, and `shop_hardware` app-plane entries are
full synthetic Shopify **Hydrogen** storefront websites (from the shopgym
dataset), branchable through StateFork's **Waypoint** backend. Selecting one in
the control panel makes Waypoint `buildah bud` the shop image, launch the
storefront inside a managed session, and CRIU-checkpoint the whole process tree;
the live site is embedded in the workspace iframe.

One command (on the VM) brings up the control plane with the shops:

```bash
cd ~/Branching_Webdemo
. .venv/bin/activate            # python3 -m venv .venv && pip install -e ".[dev]" if missing
./scripts/run-shopgym-statefork.sh
```

The launcher satisfies the host prerequisites the shop containers need and then
starts the control plane in StateFork build mode (default app `shop_clothing`;
switch shops in the UI). Open `http://127.0.0.1:8000` (or `:18000` through the
SSH tunnel).

Prerequisites the launcher checks/sets:

- `kernel.io_uring_disabled=2` — CRIU 4.x cannot checkpoint Node 22's io_uring.
- Shop base images in **root** podman storage. Restore them once from the
  shopgym archive: `~/shopgym/restore.sh` (unzips `shop_docker_images.zip` to
  `~/shopgym/docker-images/*.tar.gz`), then the launcher `sudo podman load`s them.
- A `waypoint` binary built with the Node-friendly CRIU dump flags
  `--force-irmap` and `--link-remap` (in `Andy_Waypoint/pkg/waypoint/memory.go`),
  plus the `bash_init` helper. The launcher builds both and points StateFork at
  them via `WAYPOINT_BIN` / `WAYPOINT_BASH_INIT_SRC`.

How a shop runtime is shaped (see each shop's `Dockerfile` + `statefork.yaml`):

- The Dockerfile prebundles the mock Storefront API to plain JS (`mockapi.cjs`)
  — running it under `tsx` is not CRIU-checkpointable — and bakes a
  `/app/run-shop.sh` launcher.
- `run-shop.sh` starts the prebundled mock API on `:4000` and the Hydrogen
  storefront (`node server.mjs`) on `$PORT`, so the embedded UI is the real shop
  **website**, not the bare GraphQL API. Both are plain `node`, so Waypoint/CRIU
  can dump the whole tree.
- Hydrogen emits root-relative URLs (`/assets/...`, `/collections/...`). The
  control plane has a catch-all fallback route that forwards any otherwise
  unmatched path to the active runtime, so the storefront's assets and
  navigation resolve on the single control-plane origin (works through the
  Cloudflare/SSH tunnels). db-backed apps (email/inventory) use relative URLs
  under `/runtime/` and never hit this fallback.

Product images ship only as runtime bind-mounts in the standalone shopgym setup
(`~/shopgym/mock_*/.../images`), not inside the container images, so the
StateFork build path would 404 every product picture. Bake them into the base
images once with:

```bash
./scripts/setup-shopgym-images.sh   # idempotent; copies images into /app/data/images
```

Run it after `~/shopgym/restore.sh` (which produces the base images) and rebuild
the workspace (Reset in the UI) to pick them up.

## Recommended VM Start

On `sf-exp`, prefer the Docker build-mode launcher:

```bash
cd ~/Web_Demo_For_Checkpointlite
. .venv/bin/activate
./scripts/run-statefork-docker.sh
```

If port `8000` or the runtime ports are already occupied:

```bash
./scripts/cleanup-statefork-demo.sh
./scripts/run-statefork-docker.sh
```

The UI should show:

```text
statefork / statefork:ckpt_build / Docker build
```

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

Edit `.env` and replace `DEMO_AUTH_PASSWORD` with a real demo password:

```bash
nano .env
```

Minimum required value:

```bash
DEMO_AUTH_PASSWORD=<shared-demo-password>
```

`DEMO_AUTH_USER` defaults to `demo`. The password protects the main app
with HTTP Basic Auth. The active runtime app is still an internal VM-only process
on `127.0.0.1:8300`, so the main app can manage branch environments without
opening the branch port publicly.

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

### 1. Open An Auto-Reconnecting Tunnel From Your Laptop

Run this on your laptop in a dedicated tunnel terminal. It uses tunnel-only
mode (`-N`) and retries when the SSH connection drops, which is useful when your
laptop sleeps and later wakes back up:

```bash
while true; do
  echo "[$(date)] starting SSH tunnel to sf-exp"
  ssh \
    -N \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=2 \
    -o TCPKeepAlive=no \
    -L 18000:127.0.0.1:8000 \
    -L 18300:127.0.0.1:8300 \
    sf-exp
  echo "[$(date)] SSH tunnel stopped; retrying in 5 seconds"
  sleep 5
done
```

Port meanings:

- `18000`: forwards your laptop's `127.0.0.1:18000` to the VM main FastAPI app
  on `127.0.0.1:8000`
- `18300`: forwards your laptop's `127.0.0.1:18300` to the VM StateFork
  runtime app on `127.0.0.1:8300` for direct runtime debugging

The current StateFork backend supports one active runtime at a time. The
workspace controller owns that runtime for the UI.

Using `18000` and `18300` avoids colliding with a local copy of this demo that
may already be running on your laptop. The `ExitOnForwardFailure=yes` option
makes SSH fail immediately if a requested local port is already occupied, instead
of silently leaving you connected to the wrong server. `ServerAliveInterval` and
`ServerAliveCountMax` make SSH notice dead sleep/wake connections quickly so the
loop can reconnect.

### 2. Prepare The Repo On The VM

Open a second terminal for VM commands:

```bash
ssh sf-exp
```

Inside that VM shell:

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

Still inside the SSH session, use the Docker build-mode launcher. This is the
recommended demo path:

```bash
cd ~/Web_Demo_For_Checkpointlite
. .venv/bin/activate
./scripts/run-statefork-docker.sh
```

The script sets the StateFork environment, enables `DEMO_STATEFORK_BUILD=1`,
uses this repo's `Dockerfile`, and starts the controller on `127.0.0.1:8000`.

If port `8000` is already in use, clean up the previous demo first:

```bash
./scripts/cleanup-statefork-demo.sh
./scripts/run-statefork-docker.sh
```

Manual equivalent, mostly for debugging:

```bash
cd ~/Web_Demo_For_Checkpointlite
. .venv/bin/activate

export DEMO_STATEFORK_BUILD=1
export DEMO_STATEFORK_ROOT=/users/alexxjk/Andy_StateFork
export DEMO_STATEFORK_CWD=/users/alexxjk/Andy_StateFork
export DEMO_STATEFORK_METHOD=ckpt_build
export CHECKPOINT_SESSIONS_DIR=/tmp/checkpoint-sessions-mailbox-demo
export DEMO_BRANCH_HOST=127.0.0.1
export DEMO_BRANCH_PORT_START=8300
export PYTHONPATH=src

sudo -E .venv/bin/uvicorn agent_safe_demo.control_plane.main:app --host 127.0.0.1 --port 8000
```

In Docker build mode, StateFork calls checkpoint-lite build mode against this
repo's `Dockerfile`. The mailbox app is still the ordinary runtime app
(`agent_safe_demo.app_plane.email_service.app:app`); Docker is only used by
checkpoint-lite to prepare the managed environment from the outside.

### 4. Open The UI Locally

On your laptop, open:

```text
http://127.0.0.1:18000
```

Try this flow:

```text
Run Agent -> Snapshot -> Restore Initial checkpoint -> Snapshot again
```

The header should show `statefork / statefork:ckpt_build`. The
`Runtime & Checkpoint Stats` panel shows the active backend, runtime branch,
visible checkpoint nodes, and measured snapshot/restore calls for the current
server process.

The runtime URL should look like:

```text
http://127.0.0.1:8300
```

With the safer tunnel above, open the branch locally as:

```text
http://127.0.0.1:18300
```

The app may still display the VM-side runtime URL `http://127.0.0.1:8300`.
Manually replace local port `8300` with `18300` in your browser.

### 5. Avoid Accidentally Opening A Local Demo

If you forwarded ports but still see an unexpected version, your laptop may
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
- Runtime branch IDs start with `sf-`.
- The runtime app uses VM port `8300`, viewed locally through `18300`.

### 6. Optional Smoke Test On The VM

In another SSH session or after stopping the server:

```bash
cd ~/Web_Demo_For_Checkpointlite
. .venv/bin/activate
BASE_URL=http://127.0.0.1:8000 SMOKE_TIMEOUT=120 python scripts/smoke-test.py
```

This smoke test covers the mailbox workspace flow. Expected result:

```text
agent_action_statuses: labeled, moved, draft, received, archived
branch mailbox after agent: changed
main mailbox after agent: unchanged
```

### 7. Cleanup

If a run is interrupted, clean up ports and checkpoint-lite/StateFork session
mounts:

```bash
./scripts/cleanup-statefork-demo.sh
```

## Bootstrap A Fresh Shared VM

Use this section when `sf-exp` points to a newly rebuilt VM with the same OS
configuration as the current shared VM but no project files.

Assumptions:

- You can SSH to the VM with `ssh sf-exp`.
- You have `sudo` on the VM.
- Your GitHub SSH key can access this private repo.
- The StateFork source is available at `/users/alexxjk/Andy_StateFork`, or you can
  clone it there.
- The checkpoint-lite source or binary is available at
  `/users/alexxjk/Andy_checkpoint-lite`, or you can clone/build it there.

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
  golang-go \
  docker.io

sudo criu check
sudo docker version
```

`sudo criu check` should print success. If it fails, checkpoint-lite process
checkpointing is not ready on that VM.

Docker is only required for `DEMO_STATEFORK_BUILD=1`. The default StateFork init
mode can run without building the repo's Docker image.

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

If `/users/alexxjk/Andy_checkpoint-lite/checkpoint-lite` already exists:

```bash
/users/alexxjk/Andy_checkpoint-lite/checkpoint-lite version
```

If checkpoint-lite is missing, clone/build it on the VM:

```bash
cd /users/alexxjk
git clone git@github.com:AndyGE44/Andy_checkpoint-lite.git
cd Andy_checkpoint-lite
go build -o checkpoint-lite ./cmd/waypoint
go build -o bash_init cmd/bash-init/main.go
./checkpoint-lite version
```

### 5. Prepare StateFork

If `/users/alexxjk/Andy_StateFork` already exists:

```bash
cd /users/alexxjk/Andy_StateFork
git pull --ff-only
```

If StateFork is missing, clone it on the VM:

```bash
cd /users/alexxjk
git clone git@github.com:AndyGE44/Andy_StateFork.git
cd Andy_StateFork
```

The shared VM demo expects `DEMO_STATEFORK_ROOT` and `DEMO_STATEFORK_CWD` to point
at this directory.

### 6. Verify OverlayFS With Checkpoint-Lite

On the VM:

```bash
sudo umount -l /tmp/checkpoint-sessions/*/work 2>/dev/null || true
sudo rm -rf /tmp/checkpoint-sessions /tmp/checkpoint-sessions-info

mkdir -p /tmp/ckpt-lite-min
echo hello > /tmp/ckpt-lite-min/hello.txt

sudo env CHECKPOINT_SESSIONS_DIR=/tmp/checkpoint-sessions \
  /users/alexxjk/Andy_checkpoint-lite/checkpoint-lite \
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

### 7. Verify Docker Build Mode

Use this when you specifically want to prove the Dockerfile path. First verify
the image builds:

```bash
cd ~/Web_Demo_For_Checkpointlite
sudo docker build -t agent-safe-mailbox:manual-check .
```

Then start the demo with the recommended launcher:

```bash
./scripts/run-statefork-docker.sh
```

The first request to `/api/workspace` may take longer because checkpoint-lite is
building from the Dockerfile:

```bash
curl -fsS http://127.0.0.1:8000/api/workspace
```

Expected signs that Docker build mode is working:

- The top status pill shows `statefork / statefork:ckpt_build / Docker build`.
- The `Runtime & Checkpoint Stats` panel shows `StateFork Mode` as
  `Docker build` with hint `Dockerfile enabled`.
- `GET /api/backend` still reports `statefork / statefork:ckpt_build`.
- The workspace response includes a runtime URL such as
  `http://127.0.0.1:8300`.
- `python scripts/smoke-test.py` passes with the mailbox agent actions.

Important: Docker build mode does not mean the mailbox app receives branching
APIs. The Docker image still runs the plain mailbox app; StateFork/checkpoint-lite
does snapshot and restore from the outside.

## StateFork Backend Quick Reference

This is the preferred backend for the shared VM demo. It uses StateFork's Python
controller API instead of calling checkpoint-lite directly from the web app. The
UI and FastAPI endpoints stay the same because the controller always uses
StateFork:

```bash
./scripts/run-statefork-docker.sh
```

The launcher sets `DEMO_STATEFORK_BUILD=1`, `DEMO_APP_ID`, app DB paths,
StateFork paths, runtime ports, `CHECKPOINT_SESSIONS_DIR`, and `PYTHONPATH`.

`StateForkBackend` currently calls StateFork's `snapshot`, `restore`,
`create_env_from_snapshot`, and `cleanup` methods. The control UI embeds the
selected runtime app, shows manual checkpoints, and runs the selected app's
deterministic agent flow when one is registered.

StateFork is intentionally treated as a single-active-branch backend in this
prototype. The app rejects a second running StateFork branch until the existing
branch is committed or discarded.

Commit no longer copies a branch SQLite database back over the seed database.
Instead, commit creates a StateFork snapshot from the branch state, restores that
snapshot as the managed environment, and marks the base as the controller's new
StateFork head. If another base becomes head while a branch is running, commit
rejects the stale branch and asks you to create a new branch from the current
head.

Target lifecycle:

```text
create base   -> StateFork init mode: create manager -> snapshot
              -> StateFork Docker build mode: checkpoint-lite build Dockerfile
                 -> reuse build manager's initial snapshot
create branch -> StateFork restore <base-id>
              -> StateFork create_env_from_snapshot <base-id>
              -> start runtime app URL in the forked environment
run agent     -> deterministic app-specific agent actions inside the branch
status        -> /api/backend reports statefork:<method> and snapshot/restore stats
discard       -> terminate runtime app and cleanup StateFork environment
commit        -> StateFork snapshot + restore, then advance controller head
reset         -> delete active branches, bases, sessions, and reset selected app DB
```

The same `Runtime & Checkpoint Stats` UI and `GET /api/backend` endpoint report
the active StateFork method as `statefork:<method>`.

## Repository Layout

```text
agent_safe_demo/
├── src/agent_safe_demo/
│   ├── control_plane/         # Workspace controller, branch backends, static UI
│   └── app_plane/
│       ├── email_service/     # Independent managed email app
│       ├── inventory_service/ # Independent managed inventory app
│       ├── kv_service/        # Tiny KV app (wrapper-script launch)
│       ├── shop_clothing/     # Shopify Hydrogen storefront (Dockerfile + manifest)
│       ├── shop_cookware/     # Shopify Hydrogen storefront
│       └── shop_hardware/     # Shopify Hydrogen storefront
├── tests/                     # API tests
├── docs/                      # Ubuntu / checkpoint-lite setup notes
├── scripts/                   # Local run and smoke-test helpers
├── pyproject.toml             # Python project metadata and dependencies
├── requirements.txt           # Convenience install entrypoint
└── README.md                  # Project overview
```

Generated runtime data is ignored by git:

```text
demo_mailbox.db
demo_inventory.db
.branches/
build/
dist/
```

## Useful Endpoints

Registered app-plane endpoints, served by the managed runtime:

- `GET /api/mailbox` for the email app
- `GET /api/inventory` for the inventory app
- `GET /api/messages`
- `GET /api/messages/{message_id}`
- `GET /api/state`
- `POST /api/reset`

Workspace controller endpoints, served by the main controller:

- `GET /api/apps`
- `POST /api/apps/{app_id}/select`
- `GET /api/workspace`
- `GET /api/workspace/dirty`
- `POST /api/workspace/run-agent`
- `POST /api/workspace/snapshots`
- `POST /api/workspace/restore`
- `POST /api/workspace/reset`
- `GET /api/backend`
- `GET /api/bases`
- `POST /api/bases`
- `DELETE /api/bases/{base_id}`
- `POST /api/bases/{base_id}/branches`
- `GET /api/branches`
- `POST /api/branches`
- `POST /api/branches/{branch_id}/commit`
- `POST /api/branches/{branch_id}/discard`

Base/branch endpoints are still available for compatibility and tests, but the
preferred UI path uses `/api/workspace`. The controller intentionally does not
serve app business APIs such as `/api/mailbox` or `/api/inventory`, and the
business apps intentionally do not serve `/api/workspace`.

The generated OpenAPI docs are available at `/docs`.

## Local Development

Local development still runs the StateFork-only controller. A real workspace
requires StateFork to be available, so use the shared VM for full branch,
snapshot, and restore flows.

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

The controller starts local runtime copies from the selected app registry entry.
Set `DEMO_APP_ID=email` or `DEMO_APP_ID=inventory` before startup to choose the
initial app; the UI can switch apps at runtime.

Run tests:

```bash
pytest -q
```

For an end-to-end smoke test while the dev server is running:

```bash
python scripts/smoke-test.py
```
