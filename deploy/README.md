# One-command deploy (fresh CloudLab node)

Brings up the shopgym StateFork web demo on a clean node in the **same CloudLab
project** (so the 3 GB shopgym archive is read from the project NFS). For a demo
of "the web app **and** its deployment", this script *is* the deployment story:
a bare node → a working CRIU checkpoint/restore storefront, served the
recommended way (tunnel + auth + auto-teardown).

## What it does

`deploy/deploy.sh` runs five steps (see the script for detail):

1. Install host packages — `podman buildah criu golang-go python3-venv …`.
2. Clone + **pin** the sibling repos to the exact verified commits
   (`deploy/versions.env`): `Andy_StateFork`, `Andy_Waypoint` (the branch with the
   Node-friendly CRIU dump flags). It does **not** touch `Andy_harbor` (unrelated).
3. Copy the shopgym archive from NFS → `~/shopgym` and run `~/shopgym/restore.sh`
   (unzips mock data + shop image tarballs).
4. Build artifacts — the Python venv, `waypoint` + `bash_init`, and bake product
   images into the base images (`scripts/setup-shopgym-images.sh`).
5. Serve via `deploy/serve-public.sh` — localhost bind + Cloudflare HTTPS tunnel
   + Basic Auth + auto-teardown. It prints the public URL and the login.

## Usage

On the fresh node, with an ssh-agent forwarded that can read the private repos:

```bash
git clone -b feature/shopgym-slim git@github.com:AndyGE44/Branching_Webdemo.git
cd Branching_Webdemo
./deploy/deploy.sh                 # provision + build + serve publicly
# or: ./deploy/deploy.sh --no-launch   # stop after building
```

After `--no-launch`, start it later with `./deploy/serve-public.sh`
(recommended) or `./scripts/run-shopgym-statefork.sh` (local quick test,
binds `127.0.0.1` only). Different node performance is expected and fine — only
the build / cold-start / snapshot-restore timings change; the demo behaves the
same.

## Serving (tunnel + auth + auto-teardown)

The control plane runs as **root** (CRIU/podman need it), so it must never sit
on a public port unauthenticated. `serve-public.sh` is the recommended way to
serve it:

```bash
./deploy/serve-public.sh          # start; prints the https URL + login
DEMO_TTL_HOURS=8 ./deploy/serve-public.sh
./deploy/teardown.sh              # stop everything now (or wait for the timer)
```

It:
- ensures a strong `DEMO_AUTH_PASSWORD` in `.env` (generates one if missing) and
  turns on Basic Auth across the whole app — the login also covers the embedded shops;
- binds the app to `127.0.0.1` only (never a public port) and exposes it through a
  **Cloudflare quick tunnel** (HTTPS), installing `cloudflared` if needed;
- schedules an **auto-teardown** after `DEMO_TTL_HOURS` (default 24h) via `systemd-run`,
  so a forgotten demo stops itself; `teardown.sh` also cancels the timer.

`.env` (gitignored) holds the password. The branch runtimes already bind
`127.0.0.1` only, so they are never exposed. If you instead need a raw `IP:8000`
(no tunnel), front it with a host firewall allowlist (nftables/ufw: default-deny
inbound, allow SSH + `:8000` from known source IPs), keep Basic Auth on, and set
`DEMO_MAIN_HOST=0.0.0.0` — the launcher refuses an unauthenticated public bind.

## Reproducibility

`deploy/versions.env` holds the pinned commit SHAs and the shopgym archive path.
To move the demo to a different commit, edit those values. Overrides:

- `SHOPGYM_SRC=/path/to/shopgym` — archive somewhere other than the default NFS path.
- `DEPLOY_WORKDIR=/path` — where the sibling repos are cloned (default `$HOME`, which
  is what the launcher's `DEMO_STATEFORK_ROOT` / `WAYPOINT_SRC` defaults expect).

## Notes

- Needs `sudo` (CRIU and podman run as root).
- The sibling-repo clone uses SSH; forward your ssh-agent or pre-clone the repos.
- This replaces the older NFS `bootstrap-vm.sh` for demo bring-up: it is versioned
  with the code, pins exact commits, and skips the Claude-state/`Andy_harbor` steps.
