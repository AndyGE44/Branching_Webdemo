# Shopgym StateFork Web Demo

A FastAPI **control plane** (`agent_safe_demo.control_plane.main:app`) that embeds
three Shopify **Hydrogen** mock storefronts — `shop_clothing`, `shop_cookware`,
and `shop_hardware` (from the shopgym dataset) — as branchable apps, and wraps
them in an agent-safe snapshot/restore workflow:

```text
open workspace -> initial snapshot -> user changes (cart, AI Pick) -> snapshot/restore
```

Each shop is a full synthetic storefront **website**, not a bare GraphQL API.
Selecting one in the control panel makes the StateFork **Waypoint** backend
(`DEMO_STATEFORK_METHOD=ckpt_build`) `buildah`-build the shop image, launch the
storefront inside a managed session, and CRIU-checkpoint the **whole process
tree** — including the in-memory cart. The live site is embedded in the workspace
iframe, and snapshot/restore captures and rewinds the entire runtime, cart
included.

The App selector shows only the three shops (env `DEMO_VISIBLE_APP_IDS`). The
canonical launcher is `scripts/run-shopgym-statefork.sh`.

**AI Pick** replaces the old "Run Agent" button: it is a scripted in-app
"stylist" chat that reverts the shop to its initial snapshot, fills the cart with
a preset look, and snapshots the result — a quick demo of restore → fill cart →
snapshot. The **commit / app-head** feature is disabled in this build; its
endpoints return `403`.

## Architecture Overview

The control plane is app-agnostic: it selects an app-plane service, starts it in
a managed StateFork runtime, embeds the runtime UI, and exposes snapshot/restore
controls around it. The StateFork base/branch lifecycle runs underneath the
workspace controller.

The repo exposes a single backend: `StateForkBackend`, which calls StateFork's
Python controller API for `snapshot`, `restore`, `create_env_from_snapshot`, and
`cleanup`. With `DEMO_STATEFORK_METHOD=ckpt_build`, StateFork drives the Waypoint
backend, which builds each shop's container from its `Dockerfile` and uses CRIU
to dump/restore the running process tree (`checkpoint_exec` + build mode).

### How a shop runtime is shaped

See each shop's `Dockerfile` + `statefork.yaml`:

- The Dockerfile prebundles the mock Storefront API to plain JS (`mockapi.cjs`)
  — running it under `tsx` is not CRIU-checkpointable — and bakes a
  `/app/run-shop.sh` launcher.
- `run-shop.sh` starts the prebundled mock API on `:4000` and the Hydrogen
  storefront (`node server.mjs`) on `$PORT`, so the embedded UI is the real shop
  website. Both are plain `node`, so Waypoint/CRIU can dump the whole tree.
- Hydrogen emits root-relative URLs (`/assets/...`, `/collections/...`). The
  control plane has a catch-all fallback route that forwards any otherwise
  unmatched path to the active runtime, so the storefront's assets and
  navigation resolve on the single control-plane origin (works through the
  Cloudflare/SSH tunnels).

## Host Prerequisites

The shop containers need a few host-level things to be CRIU-checkpointable.
`scripts/run-shopgym-statefork.sh` checks/sets most of these automatically, but
they describe what the node must provide:

- **`kernel.io_uring_disabled=2`** — Node 22's libuv uses io_uring, which CRIU
  4.x cannot checkpoint. The launcher sets this via `sysctl`.
- **Shop base images in _root_ podman storage.** Waypoint builds with `buildah`
  as root, so the `FROM localhost/shop-arena-mock-*` base images must live in
  root storage. Restore them once from the shopgym archive with
  `~/shopgym/restore.sh` (unzips `shop_docker_images.zip` to
  `~/shopgym/docker-images/*.tar.gz`); the launcher then `sudo podman load`s them.
- **Waypoint built with the Node-friendly CRIU dump flags `--force-irmap` and
  `--link-remap`** (in `Andy_Waypoint/pkg/waypoint/memory.go`), plus the
  `bash_init` helper that Waypoint launches inside each built container. The
  launcher builds both and points StateFork at them via `WAYPOINT_BIN` /
  `WAYPOINT_BASH_INIT_SRC`. Without those flags CRIU cannot dump the shop's
  inotify watches.
- **CRIU** and **Go** installed on the host.
- A Python venv: `python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'`.

### Bake product images into the base images

Product images ship only as runtime bind-mounts in the standalone shopgym setup
(`~/shopgym/mock_*/.../images`), **not** inside the container images, so the
StateFork build path would 404 every product picture. Bake them into the base
images once with:

```bash
./scripts/setup-shopgym-images.sh   # idempotent; copies images into /app/data/images
```

Run it after `~/shopgym/restore.sh` (which produces the base images), then
rebuild the workspace (Reset in the UI) to pick them up.

## Running The Demo

**Fresh node?** One command provisions and launches everything — installs host
packages, clones + pins the sibling repos, restores the shop images, builds the
artifacts, and starts the control plane. See [`deploy/README.md`](deploy/README.md):

```bash
git clone -b feature/shopgym-slim git@github.com:AndyGE44/Branching_Webdemo.git
cd Branching_Webdemo
./deploy/deploy.sh                 # provision + build + launch  (--no-launch to stop after building)
```

**Already provisioned** (venv + `~/Andy_StateFork` + `~/Andy_Waypoint` + `~/shopgym`
present)? Just launch the control plane:

```bash
cd ~/Branching_Webdemo
. .venv/bin/activate            # see "Host Prerequisites" if the venv is missing
./scripts/run-shopgym-statefork.sh
```

The launcher:

1. **Clean slate** — kills leftover storefront process trees (orphaned
   mock-api/Hydrogen processes that would serve a stale in-memory cart), frees
   the control-plane port and branch port range, wipes on-disk
   checkpoint/restore session state, and drops the control-plane metadata db so a
   new run starts with no prior head.
2. Sets `kernel.io_uring_disabled=2`.
3. Ensures the shop base images are in root podman storage (`sudo podman load`).
4. Builds Waypoint + `bash_init` (verifying the `--force-irmap` flag is present).
5. Launches the control plane in StateFork build mode on port `8000`, bound on
   all interfaces by default (override with `DEMO_MAIN_HOST=127.0.0.1`).

Default app is `shop_clothing`; switch shops in the UI. Open
`http://<host>:8000` (or `http://127.0.0.1:18000` through the SSH tunnel below).

Override any path/port with the matching env var: `DEMO_STATEFORK_ROOT`,
`WAYPOINT_SRC`, `SHOPGYM_DIR`, `DEMO_MAIN_HOST`, `DEMO_MAIN_PORT`, `DEMO_APP_ID`,
`DEMO_VISIBLE_APP_IDS`, `DEMO_BRANCH_PORT_START`, ...

### Related repos and data

- `Andy_StateFork` — the StateFork controller (`DEMO_STATEFORK_ROOT` /
  `DEMO_STATEFORK_CWD` point here).
- `Andy_Waypoint` — the Waypoint CRIU backend, on the branch carrying the
  Node-friendly dump flags (`--force-irmap`, `--link-remap`).
- `~/shopgym` — the data/image archive: `restore.sh`, `shop_docker_images.zip`,
  and the `mock_*` product-image zips.

### Cleanup

If a run is interrupted, free ports and StateFork/Waypoint session mounts:

```bash
./scripts/cleanup-statefork-demo.sh
```

## Public Demo (tunnel + auth + auto-teardown)

The control plane runs as **root** (CRIU/podman), so for a timed public demo do
**not** bind the port directly. Use the wrapper: it binds the app to `127.0.0.1`,
exposes it through a Cloudflare quick tunnel (HTTPS), enforces Basic Auth, and
schedules an automatic teardown.

```bash
./deploy/serve-public.sh            # prints the https URL + login
DEMO_TTL_HOURS=8 ./deploy/serve-public.sh
./deploy/teardown.sh                # stop everything now (or wait for the timer)
```

`serve-public.sh`:

- ensures a strong `DEMO_AUTH_PASSWORD` in `.env` (generates one if missing) and
  turns on Basic Auth across the whole app — the login also covers the embedded
  shops. Set your own by putting `DEMO_AUTH_USER` / `DEMO_AUTH_PASSWORD` in `.env`
  (copy `.env.example`) before starting; `.env` is gitignored.
- installs `cloudflared` if needed, opens a `*.trycloudflare.com` tunnel, and prints
  the URL. Quick Tunnel URLs are ephemeral (they change on restart) — short demos only.
- schedules an auto-teardown after `DEMO_TTL_HOURS` (default 24h) so a forgotten demo
  stops itself; `teardown.sh` also cancels the timer.

Keep the URL and password private (anyone with both reaches the demo). For a raw
`IP:8000` instead of the tunnel, front it with a host firewall allowlist — see
[`deploy/README.md`](deploy/README.md).

### Driving the tunnel manually

`serve-public.sh` just runs `scripts/run-shopgym-statefork.sh` (bound to localhost)
alongside `scripts/run-cloudflare-quick-tunnel.sh`. To do it by hand: start the
control plane with `DEMO_MAIN_HOST=127.0.0.1`, then

```bash
tmux new -d -s cf-shopgym './scripts/run-cloudflare-quick-tunnel.sh'
tmux capture-pane -pt cf-shopgym -S -80     # grab the trycloudflare.com URL
tmux kill-session -t cf-shopgym             # stop the tunnel
```

## Shared VM Demo With SSH Port Forwarding

The demo is tested on a shared Ubuntu VM reachable as `ssh sf-exp`. Use SSH port
forwarding to view the VM-hosted demo from your local browser without opening
public inbound ports.

### 1. Open An Auto-Reconnecting Tunnel From Your Laptop

Run this on your laptop in a dedicated tunnel terminal. It uses tunnel-only mode
(`-N`) and retries when the SSH connection drops (useful across laptop
sleep/wake):

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

- `18000` → VM control plane on `127.0.0.1:8000`.
- `18300` → VM branch runtime (the active storefront) on `127.0.0.1:8300+` for
  direct runtime debugging.

`ExitOnForwardFailure=yes` makes SSH fail immediately if a requested local port
is already occupied, instead of silently connecting you to the wrong server. The
StateFork backend serves one active runtime at a time; the workspace controller
owns it for the UI.

### 2. Start The Demo On The VM

In a second terminal:

```bash
ssh sf-exp
cd ~/Branching_Webdemo
. .venv/bin/activate
./scripts/run-shopgym-statefork.sh
```

### 3. Open The UI Locally

On your laptop, open:

```text
http://127.0.0.1:18000
```

Try this flow:

```text
AI Pick -> Snapshot -> Restore Initial snapshot -> Snapshot again
```

The `Runtime & Checkpoint Stats` panel shows the active backend, runtime branch,
visible checkpoint nodes, and measured snapshot/restore calls for the current
server process. Runtime branch IDs start with `sf-`. If the UI shows a VM-side
runtime URL like `http://127.0.0.1:8300`, replace local port `8300` with `18300`
in your browser.

### 4. Avoid Accidentally Opening A Local Demo

If you forwarded ports but still see an unexpected version, your laptop may
already be listening on the same port:

```bash
lsof -iTCP:8000 -sTCP:LISTEN -n -P
```

Stop local demo servers before testing the VM:

```bash
lsof -tiTCP:8000 -sTCP:LISTEN | xargs -r kill
lsof -tiTCP:8300-8350 -sTCP:LISTEN | xargs -r kill
```

## Bootstrap A Fresh Shared VM

Use this when `sf-exp` points to a newly rebuilt VM with the same OS as the
current shared VM but no project files.

Assumptions:

- You can `ssh sf-exp` and have `sudo` on the VM.
- Your GitHub SSH key can access the private repos.
- `Andy_StateFork` and `Andy_Waypoint` are available (or cloneable) under
  `/users/alexxjk`, and the `~/shopgym` data/image archive is present.

### 1. Install System Packages

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
  podman \
  buildah \
  unzip

sudo criu check
```

`sudo criu check` should print success. If it fails, CRIU process checkpointing
is not ready on that VM.

### 2. Clone The Repos

```bash
cd ~
git clone git@github.com:AndyGE44/Branching_Webdemo.git

cd /users/alexxjk
git clone git@github.com:AndyGE44/Andy_StateFork.git
git clone git@github.com:AndyGE44/Andy_Waypoint.git   # branch with the CRIU node-dump flags
```

### 3. Prepare The Python Environment

```bash
cd ~/Branching_Webdemo
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

### 4. Prepare StateFork And Waypoint

`run-shopgym-statefork.sh` builds Waypoint + `bash_init` for you, but the
Waypoint checkout must already carry the Node-friendly CRIU dump flags
(`--force-irmap`, `--link-remap`) in `pkg/waypoint/memory.go`; the launcher
refuses to start otherwise. Confirm StateFork is on the expected branch:

```bash
cd /users/alexxjk/Andy_StateFork && git pull --ff-only
cd /users/alexxjk/Andy_Waypoint  && git pull --ff-only
```

### 5. Restore The Shopgym Data And Bake Images

```bash
~/shopgym/restore.sh                       # produces the shop base images
cd ~/Branching_Webdemo
./scripts/setup-shopgym-images.sh          # bakes product images into the base images
```

Then run the demo as in "Running The Demo".

## StateFork Backend Quick Reference

`StateForkBackend` uses StateFork's Python controller API instead of calling the
checkpoint backend directly from the web app. The launcher sets
`DEMO_STATEFORK_BUILD=1`, `DEMO_STATEFORK_METHOD=ckpt_build`, the StateFork
paths, `WAYPOINT_BIN` / `WAYPOINT_BASH_INIT_SRC`, the runtime ports,
`CHECKPOINT_SESSIONS_DIR`, `DEMO_APP_ID`, `DEMO_VISIBLE_APP_IDS`, and
`PYTHONPATH`.

StateFork is a single-active-branch backend in this prototype: the app rejects a
second running branch until the existing one is discarded.

Lifecycle (build mode):

```text
create base   -> Waypoint build mode: buildah builds the shop Dockerfile
              -> reuse the build manager's initial snapshot
create branch -> StateFork restore <base-id>
              -> StateFork create_env_from_snapshot <base-id>
              -> start the storefront URL in the forked environment
AI Pick       -> scripted: restore initial snapshot, fill cart, snapshot
snapshot      -> StateFork snapshot of the live process tree (cart included)
restore       -> StateFork restore of a checkpoint node
status        -> /api/backend reports statefork:ckpt_build and snapshot/restore stats
discard       -> terminate runtime and cleanup the StateFork environment
reset         -> delete active branches, bases, sessions, head metadata; clear cart cookie
commit        -> DISABLED (returns 403)
```

The `Runtime & Checkpoint Stats` UI and `GET /api/backend` report the active
StateFork method as `statefork:ckpt_build`.

## Repository Layout

```text
Branching_Webdemo/
├── src/agent_safe_demo/
│   ├── control_plane/         # Workspace controller, StateFork backend, static UI
│   └── app_plane/
│       ├── shop_clothing/     # Shopify Hydrogen storefront (Dockerfile + statefork.yaml)
│       ├── shop_cookware/     # Shopify Hydrogen storefront
│       └── shop_hardware/     # Shopify Hydrogen storefront
├── tests/                     # API tests
├── docs/                      # Ubuntu / checkpoint setup notes
├── scripts/                   # Run and helper scripts
├── pyproject.toml             # Python project metadata and dependencies
├── requirements.txt           # Convenience install entrypoint
└── README.md                  # This file
```

Generated runtime data is ignored by git:

```text
control_plane_metadata.db
.branches/
build/
dist/
```

### Scripts

- `deploy/deploy.sh` — one-command bring-up on a fresh node (provision + clone/pin
  repos + restore images + build + launch). See `deploy/README.md`.
- `deploy/serve-public.sh` — timed public demo: localhost + Cloudflare tunnel +
  Basic Auth + auto-teardown. `deploy/teardown.sh` stops it.
- `scripts/run-shopgym-statefork.sh` — canonical launcher (clean slate, host
  prereqs, build Waypoint, load images, launch control plane on `:8000`).
- `scripts/setup-shopgym-images.sh` — bake product images into the base images.
- `scripts/run-dev.sh` — lightweight local dev runner (auto-reload, no
  StateFork; UI only).
- `scripts/run-cloudflare-quick-tunnel.sh` — public quick-tunnel helper.
- `scripts/cleanup-statefork-demo.sh` — free ports and clean session mounts.

## Useful Endpoints

Workspace controller endpoints (served by the control plane):

- `GET /api/apps`
- `POST /api/apps/{app_id}/select`
- `GET /api/workspace`
- `GET /api/workspace/dirty`
- `POST /api/workspace/snapshots`
- `POST /api/workspace/restore`
- `POST /api/workspace/reset`
- `GET /api/workspace/commits`
- `GET /api/backend`
- `GET /api/bases`
- `POST /api/bases`
- `DELETE /api/bases/{base_id}`
- `POST /api/bases/{base_id}/branches`
- `GET /api/branches`
- `POST /api/branches`
- `POST /api/branches/{branch_id}/discard`

Commit endpoints (`POST /api/workspace/commit`,
`POST /api/branches/{branch_id}/commit`) are intentionally **disabled** and
return `403 Commit is disabled in this build.` Any unmatched path is forwarded to
the active storefront runtime so the embedded Hydrogen site's assets and
navigation resolve on the control-plane origin.

The generated OpenAPI docs are available at `/docs`.

## Local Development

Local development runs the StateFork-only control plane UI. A real workspace
(branch / snapshot / restore) requires the StateFork + Waypoint host setup, so
use the shared VM for full flows.

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
