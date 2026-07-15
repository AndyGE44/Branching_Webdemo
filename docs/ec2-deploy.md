# Deploy the shopgym StateFork demo on a fresh EC2 node

`deploy/deploy.sh` was written for a CloudLab node in the same project (it reads
the shopgym archive from `/proj` NFS). This runbook covers the **EC2** path: no
`/proj` NFS, shopgym moved in over `rsync`, and a stable public URL via
**Elastic IP + Route 53 + Caddy** (all AWS, plus a free auto-renewing Let's
Encrypt cert).

The viewer experience is identical to any other HTTPS option: a stable
`https://demo.yourdomain.com` behind the demo's Basic Auth. The control plane
runs as **root** (CRIU/podman need it), so it stays bound to `127.0.0.1` and
Caddy is the only thing on a public port.

---

## 0. Provision the instance

- **AMI/size:** Ubuntu 22.04 or 24.04, ≥ 8 GB RAM and ≥ 4 vCPU (e.g. `m6i.xlarge`
  / `t3.xlarge`) — buildah + CRIU + the Node/Hydrogen build are not tiny.
- **Disk:** ≥ 50 GB gp3 EBS (shopgym unzips to several GB and each shop image is
  large).
- **Elastic IP:** allocate one and **associate it** with the instance. This is
  what makes the public IP survive stop/start/reboot, so the URL stays fixed.
- **Security group (inbound):**
  - `22/tcp` from your admin IP **and** from the CloudLab node's public IP (for
    the rsync push in step 1).
  - `80/tcp` + `443/tcp` from your audience (`0.0.0.0/0` for a public demo).
    Port 80 is required for Caddy's ACME HTTP-01 challenge; 443 serves the demo.
  - Nothing else — the app's `:8000` is never public (Caddy proxies to it over
    localhost).

## 1. Move shopgym in with rsync (CloudLab → EC2)

`restore.sh` rebuilds everything from the four zips, so only move those plus the
two scripts (~3.2 GB) — not the redundant pre-extracted dirs.

Run **on the CloudLab node** (where shopgym lives). You need the EC2 SSH key
here, and the EC2 security group must already allow SSH from this node:

```bash
SRC=/proj/cuserverless-PG0/share/shopgym
EC2=ubuntu@<elastic-ip>
ssh -i ~/ec2-key.pem "$EC2" 'mkdir -p ~/shopgym-src'
rsync -avhP -e "ssh -i ~/ec2-key.pem" \
  "$SRC"/mock_clothing.zip "$SRC"/mock_cookware.zip "$SRC"/mock_hardware.zip \
  "$SRC"/shop_docker_images.zip "$SRC"/restore.sh "$SRC"/shopgym.sh \
  "$EC2":~/shopgym-src/
```

`-P` makes it resumable — re-run the same command if the link drops and it picks
up where it left off. (rsync is preinstalled on Ubuntu; if not, `sudo apt install
-y rsync` on the EC2 side first, or fall back to `scp`.)

## 2. Deploy the app

SSH in with **agent forwarding** (`-A`) so the deploy can clone the private
`Alex-XJK/*` repos as you:

```bash
ssh -A ubuntu@<elastic-ip>
chmod +x ~/shopgym-src/restore.sh ~/shopgym-src/shopgym.sh
git clone git@github.com:AndyGE44/Branching_Webdemo.git
cd Branching_Webdemo && git checkout main
SHOPGYM_SRC=~/shopgym-src ./deploy/deploy.sh --no-launch
```

`deploy.sh` installs host packages, clones + pins the sibling repos from
`deploy/versions.env` (now **`Alex-XJK/StateFork@main`** and
**`Alex-XJK/waypoint@feature/session-isolation`**), restores shopgym, and builds
`waypoint`/`bash_init` + the baked shop images. `--no-launch` stops before
serving so you can set up the URL first.

> **This is the acceptance test.** Alex's forks have not been run with the
> webdemo before. If it fails at the build or first checkpoint, the likely cause
> is StateFork API drift — fall back to the known-good Andy pins (kept in the
> comments in `deploy/versions.env`: `Andy_StateFork@d9f36b0`,
> `Andy_Waypoint@b3ff442`) and re-run.

## 3. Fixed URL — Route 53 + Caddy

**Route 53:** create/confirm a hosted zone for `yourdomain.com`, then add an
**A record** `demo.yourdomain.com → <Elastic IP>`. Wait for it to resolve
(`dig +short demo.yourdomain.com` should return the Elastic IP) before the next
step, or Caddy's certificate request will fail and retry.

**Caddy** (installs the official apt repo, then Caddy):

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

Set `/etc/caddy/Caddyfile` to:

```
demo.yourdomain.com {
    reverse_proxy 127.0.0.1:8000
}
```

```bash
sudo systemctl restart caddy   # auto-provisions + auto-renews the Let's Encrypt cert
```

## 4. Run the demo permanently (localhost + Caddy, no tunnel)

```bash
cd ~/Branching_Webdemo
sudo DEMO_TUNNEL_MODE=none ./deploy/install-service.sh
```

`DEMO_TUNNEL_MODE=none` installs only the control plane (bound to `127.0.0.1:8000`,
Basic Auth on, idle auto-reset), with **no cloudflared** — Caddy is your front
door. The services start on boot and restart on crash. Grab the generated login:

```bash
grep -E '^DEMO_AUTH_(USER|PASSWORD)=' ~/Branching_Webdemo/.env
```

## 5. Verify

```bash
dig +short demo.yourdomain.com          # -> your Elastic IP
curl -sI https://demo.yourdomain.com     # HTTP/2 401 + valid cert = working (401 = Basic Auth)
```

Then open `https://demo.yourdomain.com` and log in with the values from step 4.

## Ops

- **Logs:** `journalctl -u shopgym-demo.service -f` (app), `journalctl -u caddy -f` (TLS/proxy).
- **Stop now:** `./deploy/teardown.sh` (services still return on boot until you `--uninstall`).
- **Remove:** `sudo ./deploy/install-service.sh --uninstall`.
- **Move to different repo versions:** edit `deploy/versions.env`, re-run `deploy.sh`.
