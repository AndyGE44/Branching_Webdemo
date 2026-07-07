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

**AI Pick** is a scripted in-app "stylist" chat (no real model): it reverts the
shop to its initial snapshot, fills the cart with a preset look, and snapshots
the result — a quick demo of restore → fill cart → snapshot.

## How To Run

The control plane runs as **root** (CRIU/podman need it), so how you start it is
a security decision:

| Path | Script | Use when |
|---|---|---|
| **Recommended** | `./deploy/serve-public.sh` | Anyone other than you will reach the demo. Binds localhost, exposes via a Cloudflare HTTPS tunnel, enforces Basic Auth, auto-tears-down after `DEMO_TTL_HOURS`. |
| **Run permanently** | `sudo ./deploy/install-service.sh` | The demo should stay up long-term. Installs systemd services that start on boot and restart on crash, with no auto-teardown. |
| Fresh node | `./deploy/deploy.sh` | Nothing is provisioned yet. Installs packages, clones+pins sibling repos, restores images, builds, then runs `serve-public.sh`. |
| **Quick test only** | `./scripts/run-shopgym-statefork.sh` | You are testing locally (or through an SSH tunnel). Binds `127.0.0.1` only and refuses a public bind without Basic Auth. |

Any of these paths also **auto-resets an idle demo**: after `DEMO_IDLE_RESET_MINUTES`
(default 10) with no activity, the control plane rebuilds a clean shop — the same
Reset the UI button does. A shop already at its clean initial state is left alone,
so a pristine demo is never needlessly rebuilt. Set `DEMO_IDLE_RESET_MINUTES=0` to
disable.

### Recommended: public demo (tunnel + auth + auto-teardown)

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
- binds the app to `127.0.0.1` only — the raw port is never exposed — and opens a
  `*.trycloudflare.com` tunnel (HTTPS), installing `cloudflared` if needed. Quick
  Tunnel URLs are ephemeral (they change on restart) — short demos only.
- schedules an auto-teardown after `DEMO_TTL_HOURS` (default 24h) so a forgotten
  demo stops itself; `teardown.sh` also cancels the timer.

Keep the URL and password private (anyone with both reaches the demo). If you
instead need a raw `IP:8000` (no tunnel), front it with a host firewall
allowlist (default-deny inbound, allow SSH + `:8000` from known source IPs),
set `DEMO_AUTH_PASSWORD`, and set `DEMO_MAIN_HOST=0.0.0.0` — the quick-test
launcher refuses an unauthenticated public bind.

For a **permanent** run through this same path (no supervision, but no
teardown), set `DEMO_TTL_HOURS=0`. To also survive crashes and reboots, use the
systemd path below instead.

### Run permanently (systemd: boot start + crash restart)

```bash
sudo ./deploy/install-service.sh              # quick tunnel (ephemeral URL)
sudo ./deploy/install-service.sh --uninstall  # stop, disable, remove
```

Installs two services — `shopgym-demo.service` (control plane, root) and
`shopgym-demo-tunnel.service` (tunnel) — both `enable`d (start on boot) with
`Restart=always`. There is **no** auto-teardown. Manage them the usual way:

```bash
systemctl status shopgym-demo.service shopgym-demo-tunnel.service
journalctl -u shopgym-demo.service -f        # control-plane log (idle resets show here)
journalctl -u shopgym-demo-tunnel.service | grep trycloudflare   # the quick-tunnel URL
./deploy/teardown.sh                          # stop now (services return on reboot)
```

**Public URL.** The tunnel mode is set by `DEMO_TUNNEL_MODE`:

- `quick` (default) — free Cloudflare quick tunnel, but the `*.trycloudflare.com`
  URL **changes every time the tunnel restarts** (so not a durable link).
- `named` — a pre-created Cloudflare **named tunnel** with a **stable hostname**.
  Free if you already have a domain on Cloudflare. Create the tunnel in the
  dashboard (Zero Trust → Tunnels), then:
  `DEMO_TUNNEL_MODE=named CLOUDFLARE_TUNNEL_TOKEN=eyJ... sudo -E ./deploy/install-service.sh`
  (the token is stored `0600` in `/etc/shopgym-demo-tunnel.env`, never on the argv).
- `none` — installs only the control plane; expose `127.0.0.1:8000` yourself, e.g.
  **Tailscale Funnel** (a free, stable `https://<node>.<tailnet>.ts.net`, no domain
  needed) or a firewalled direct `IP:8000`.

> **CloudLab note:** on an ephemeral research node, "permanent" lasts as long as
> the node does — boot-start covers reboots, but the node itself is reclaimed when
> the experiment ends, which also ends any tunnel/URL.

#### Stable free URL via Tailscale Funnel (no domain)

`none` mode pairs with **Tailscale Funnel** for a free, stable public URL
(`https://<name>.<tailnet>.ts.net`) — no domain, just a free Tailscale login.
Visitors need nothing installed and Basic Auth still applies. On the demo node:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=statefork-shopify-demo   # open the printed URL to approve the node
sudo tailscale funnel --bg 8000                       # click the enable-Funnel link if prompted
DEMO_TUNNEL_MODE=none sudo -E ./deploy/install-service.sh   # control plane only (no cloudflared)
```

One-time, in the Tailscale admin console: enable **HTTPS Certificates** (DNS
settings) and approve **Funnel** for the node. The device's machine name is the
URL host — rename it under **Machines** if you want a different name (the client
`--hostname` only sets it on first join). Funnel persists in `tailscaled` (which
starts on boot), so the URL is fixed across process, tunnel, and machine
restarts. Set `DEMO_TUNNEL_MODE=none` in `.env` so a later plain
`install-service.sh` re-run does not re-add the cloudflared tunnel.

### Fresh node: one-command deploy

```bash
git clone git@github.com:AndyGE44/Branching_Webdemo.git
cd Branching_Webdemo
./deploy/deploy.sh                 # provision + build + serve (--no-launch to stop after building)
```

See [`deploy/README.md`](deploy/README.md) for what it provisions and how
versions are pinned.

### Quick test: local launcher

Use this only for local testing on an already-provisioned node (venv +
`~/Andy_StateFork` + `~/Andy_Waypoint` + `~/shopgym` present):

```bash
cd ~/Branching_Webdemo
. .venv/bin/activate            # see "Host Prerequisites" if the venv is missing
./scripts/run-shopgym-statefork.sh
```

The launcher binds `127.0.0.1:8000` and:

1. **Clean slate** — kills leftover storefront process trees (orphaned
   mock-api/Hydrogen processes that would serve a stale in-memory cart), frees
   the control-plane port and branch port range, and wipes on-disk
   checkpoint/restore session state.
2. Sets `kernel.io_uring_disabled=2`.
3. Ensures the shop base images are in root podman storage (`sudo podman load`).
4. Builds Waypoint + `bash_init` (verifying the `--force-irmap` flag is present).
5. Launches the control plane in StateFork build mode on `127.0.0.1:8000`.

Default app is `shop_clothing`; switch shops in the UI. Open
`http://127.0.0.1:8000` (or `http://127.0.0.1:18000` through the SSH tunnel
below). Configuration comes from `.env` or env vars (`DEMO_STATEFORK_ROOT`,
`WAYPOINT_SRC`, `SHOPGYM_DIR`, `DEMO_MAIN_HOST`, `DEMO_MAIN_PORT`,
`DEMO_APP_ID`, `DEMO_VISIBLE_APP_IDS`, `DEMO_BRANCH_PORT_START`, ...);
explicit env vars win over `.env`.

### Cleanup

If a run is interrupted, free ports and StateFork/Waypoint session mounts:

```bash
./scripts/cleanup-statefork-demo.sh
```

## Architecture Overview

The control plane is app-agnostic: it discovers apps from their manifests,
starts the selected one in a managed StateFork runtime, embeds the runtime UI,
and exposes snapshot/restore controls around it.

```text
src/agent_safe_demo/control_plane/
├── main.py             # FastAPI wiring + the routes the UI calls
├── workspace.py        # single-workspace controller: app + base + branch
├── statefork.py        # StateFork backend: base/branch/snapshot lifecycle
├── runtime_manager.py  # starts the app inside checkpoint-lite's managed shell
├── app_registry.py     # discovers app_plane/*/statefork.yaml -> AppSpec
├── manifest.py         # statefork.yaml schema
├── proxy.py            # storefront reverse proxy (same-origin embedding)
├── auth.py             # optional Basic Auth middleware
└── static/             # control-panel UI (index.html, app.js, styles.css)
```

`StateForkBackend` calls StateFork's Python controller API for `snapshot`,
`restore`, `create_env_from_snapshot`, and `cleanup`. With
`DEMO_STATEFORK_METHOD=ckpt_build`, StateFork drives the Waypoint backend, which
builds each shop's container from its `Dockerfile` and uses CRIU to dump/restore
the running process tree (`checkpoint_exec` + build mode).

Lifecycle (build mode):

```text
create base   -> Waypoint build mode: buildah builds the shop Dockerfile
              -> reuse the build manager's initial snapshot
create branch -> StateFork restore <base-id>
              -> StateFork create_env_from_snapshot <base-id>
              -> start the storefront in the forked environment
AI Pick       -> scripted: restore initial snapshot, fill cart, snapshot
snapshot      -> StateFork snapshot of the live process tree (cart included)
restore       -> StateFork restore of a checkpoint node
reset         -> terminate the runtime, cleanup StateFork envs, clear cart cookie
```

StateFork is a single-active-branch backend in this prototype: the app rejects a
second running branch until the workspace is reset.

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

Adding a shop = adding a directory under `app_plane/` with a `Dockerfile` and a
`statefork.yaml`; no control-plane code changes are needed.

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

### Related repos and data

- `Andy_StateFork` — the StateFork controller (`DEMO_STATEFORK_ROOT` /
  `DEMO_STATEFORK_CWD` point here).
- `Andy_Waypoint` — the Waypoint CRIU backend, on the branch carrying the
  Node-friendly dump flags (`--force-irmap`, `--link-remap`).
- `~/shopgym` — the data/image archive: `restore.sh`, `shop_docker_images.zip`,
  and the `mock_*` product-image zips.

## Shared VM Demo With SSH Port Forwarding

The demo is tested on a shared Ubuntu VM reachable as `ssh sf-exp`. Use SSH port
forwarding to view the VM-hosted demo from your local browser without opening
public inbound ports — this pairs naturally with the quick-test launcher's
localhost bind.

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
is already occupied, instead of silently connecting you to the wrong server.

### 2. Start The Demo On The VM

In a second terminal:

```bash
ssh sf-exp
cd ~/Branching_Webdemo
. .venv/bin/activate
./scripts/run-shopgym-statefork.sh
```

### 3. Open The UI Locally

On your laptop, open `http://127.0.0.1:18000` and try this flow:

```text
AI Pick -> Snapshot -> Restore Initial snapshot -> Snapshot again
```

Runtime branch IDs start with `sf-`. If the UI shows a VM-side runtime URL like
`http://127.0.0.1:8300`, replace local port `8300` with `18300` in your browser.
`GET /api/backend` reports the active backend (`statefork:ckpt_build`) and the
measured snapshot/restore timings for the current server process.

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

`./deploy/deploy.sh` automates all of this — the steps below are the manual
equivalent for when you need to deviate from it.

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
refuses to start otherwise. Confirm both repos are on the expected branches:

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

Then run the demo as in "How To Run".

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
├── deploy/                    # Recommended start path (deploy, serve, teardown)
├── scripts/                   # Quick-test launcher and helpers
├── pyproject.toml             # Python project metadata and dependencies
├── requirements.txt           # Convenience install entrypoint
└── README.md                  # This file
```

### Scripts

- `deploy/deploy.sh` — one-command bring-up on a fresh node (provision + clone/pin
  repos + restore images + build + serve). See `deploy/README.md`.
- `deploy/serve-public.sh` — **recommended start path**: localhost bind +
  Cloudflare tunnel + Basic Auth + auto-teardown. `deploy/teardown.sh` stops it.
- `scripts/run-shopgym-statefork.sh` — **quick test only**: clean slate, host
  prereqs, build Waypoint, load images, launch on `127.0.0.1:8000`.
- `scripts/setup-shopgym-images.sh` — bake product images into the base images.
- `scripts/run-dev.sh` — lightweight local dev runner (auto-reload, no
  StateFork; UI only).
- `scripts/cleanup-statefork-demo.sh` — free ports and clean session mounts.

## API

Endpoints the control panel uses (all behind Basic Auth when enabled):

- `GET /api/apps` — discovered shops + current selection
- `POST /api/apps/{app_id}/select` — switch shop (resets the workspace)
- `GET /api/workspace` — ensure/return the workspace (build base, fork branch,
  Initial snapshot on first call)
- `POST /api/workspace/snapshots` — snapshot the live runtime (capped at
  `DEMO_MAX_SNAPSHOTS`, default 20 — each snapshot is a full CRIU dump on disk;
  Reset clears them)
- `POST /api/workspace/restore` — restore a snapshot
- `POST /api/workspace/reset` — tear down and rebuild from scratch
- `GET /api/backend` — diagnostics: backend method, totals, snapshot/restore timings
- `/runtime/*` — reverse proxy to the embedded storefront

Any other unmatched path (except `/api/*` and `/static/*`) is forwarded to the
active storefront runtime so the embedded Hydrogen site's root-relative assets
and navigation resolve on the control-plane origin. The generated OpenAPI docs
are available at `/docs`.

## Local Development

Local development runs the control plane UI without StateFork. A real workspace
(branch / snapshot / restore) requires the StateFork + Waypoint host setup, so
use the shared VM for full flows.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
./scripts/run-dev.sh
```

Open `http://127.0.0.1:8000`. Run tests:

```bash
pytest -q
```
