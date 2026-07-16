# Deploy the shopgym StateFork demo on a fresh EC2 node

`deploy/deploy.sh` was written for a CloudLab node in the same project (it reads
the shopgym archive from `/proj` NFS). This is the **EC2** path: no `/proj` NFS,
shopgym copied in over the network, and a stable public URL via **Tailscale
Funnel** — free, no domain, and **no inbound ports**.

Verified end-to-end on Ubuntu 24.04 (2026-07-16), including CRIU
snapshot/restore and add-to-cart through the public URL.

> **You do not need an Elastic IP or Route 53 for the URL.** Funnel dials *out*
> to Tailscale, so the public hostname is independent of the box's IP. An
> Elastic IP is only worth it if you want a stable address for your own SSH.

---

## 0. Provision the instance

- **AMI/size:** Ubuntu 24.04, ≥ 4 vCPU / ≥ 8 GB RAM (buildah + CRIU + the
  Node/Hydrogen build are not tiny). 4 vCPU / 15 GB is comfortable.
- **Disk:** ≥ 50 GB gp3. shopgym unzips to several GB and each shop image is large.
- **Security group (inbound):** **SSH (22) only** — from your admin IP, plus the
  IP of whichever machine will push the shopgym archive. Funnel needs **nothing**
  inbound. Do not open 8000 or 8300: the shop runtime binds `0.0.0.0:8300`, so
  the security group is what keeps it private.

## 1. Copy shopgym onto the box (~3.2 GB)

`restore.sh` rebuilds everything from the four zips, so only move those plus the
two scripts — not the pre-extracted dirs.

From a machine that has the archive (e.g. a CloudLab node):

```bash
SRC=/proj/cuserverless-PG0/share/shopgym
EC2=ubuntu@<ec2-ip>
ssh "$EC2" 'mkdir -p ~/shopgym-src'
rsync -avh --info=progress2 \
  "$SRC"/mock_clothing.zip "$SRC"/mock_cookware.zip "$SRC"/mock_hardware.zip \
  "$SRC"/shop_docker_images.zip "$SRC"/restore.sh "$SRC"/shopgym.sh \
  "$EC2":~/shopgym-src/
```

Re-run the same command to resume if the link drops. (An S3 bucket works too and
is reusable for future nodes: `aws s3 cp` up, then pull down with an instance
IAM role.)

## 2. Build

All repos are public, so **no ssh-agent forwarding is needed**:

```bash
ssh ubuntu@<ec2-ip>
chmod +x ~/shopgym-src/restore.sh ~/shopgym-src/shopgym.sh
git clone https://github.com/AndyGE44/Branching_Webdemo.git
cd Branching_Webdemo
SHOPGYM_SRC=$HOME/shopgym-src ./deploy/deploy.sh --no-launch
```

That installs host packages, clones + pins the sibling repos per
`deploy/versions.env` (into `~/StateFork` and `~/waypoint`), restores shopgym,
builds `waypoint`/`bash_init`, and bakes the product images. Takes ~10–15 min.

## 3. Run it (localhost + systemd)

```bash
sudo DEMO_TUNNEL_MODE=none ./deploy/install-service.sh
grep -E '^DEMO_AUTH_(USER|PASSWORD)=' .env      # your login
```

`DEMO_TUNNEL_MODE=none` installs only the control plane — bound to
`127.0.0.1:8000`, Basic Auth on, idle auto-reset, started on boot, restarted on
crash. No cloudflared. Funnel becomes the front door next.

## 4. Public URL — Tailscale Funnel

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

**Join with an auth key — not the interactive login.** Generate a key in the
Tailscale admin console (*Settings → Keys → Generate auth key*), then:

```bash
printf '%s' 'tskey-auth-XXXX' | sudo tee /tmp/tskey >/dev/null && sudo chmod 600 /tmp/tskey
sudo tailscale up --auth-key=file:/tmp/tskey --hostname=statefork-shopify-demo
sudo shred -u /tmp/tskey
sudo tailscale funnel --bg 8000
```

`--auth-key=file:` keeps the key out of the process list. Revoke the key in the
console afterwards.

> **Why not `sudo tailscale up` interactively?** On EC2 it hangs: it sends
> `RegisterReq`, logs `controlhttp: forcing port 443 dial due to recent noise
> dial`, and never prints an auth URL (silent, even with a forced TTY).
> Outbound to Tailscale is fine (`/key` 200, `/ts2021` 400, DERP 200) — the
> auth-key path simply skips the AuthURL round-trip that stalls. Don't burn time
> debugging it.

One-time in the admin console: enable **HTTPS Certificates** (DNS settings) and
allow **Funnel** for the node (`tailscale funnel` errors with a link if not).

Your URL is the machine name: `https://<machine>.<tailnet>.ts.net`. If that name
is already held by an old (even offline) node, Tailscale silently appends `-1` —
delete the stale machine and rename *before* sharing the link, since renaming
changes the URL.

## 5. Verify

```bash
sudo tailscale status                                  # Online, shows the DNS name
curl -sI https://<machine>.<tailnet>.ts.net/           # 401 = Basic Auth = working
```

**Funnel's public DNS record can take well over 5 minutes to publish.** Don't
conclude it's broken early — query the authoritative server (`dig <name>
@ns1.dnsimple.com`) rather than a local resolver, which negative-caches misses
for 300s (the ts.net SOA minimum TTL).

Then open the URL, log in, and **add something to the cart**. That exercises the
write path (Hydrogen action → mock Storefront API); a plain page load does not,
and the two fail independently.

## Troubleshooting

- **Shop logs.** The shop runs under `chroot` with an isolated PID namespace but
  a *shared* mount namespace, so its `/tmp/sf-<id>-runtime.log` is **not** the
  host's `/tmp`. Read it through the process's own root, and expand the glob as
  root or it silently resolves to nothing:
  ```bash
  PID=$(pgrep -f 'node server.mjs' | head -1)
  sudo sh -c "cat /proc/$PID/root/tmp/sf-*.log"     # Hydrogen stack traces
  ```
  The `shell_*.log` under the Waypoint session dir is only bash_init chatter.
- **Everything loads but writes 500.** That is React Router's action guard
  comparing `x-forwarded-host` against `Origin`. `control_plane/proxy.py` strips
  that header for exactly this reason; if you front the demo with something new
  that injects other `X-Forwarded-*` headers, look there first.
- **`deploy.sh` can't find shopgym.** `SHOPGYM_SRC` must be set in the
  environment when the script runs — `deploy.sh` *sources* `versions.env`, which
  defines it as `${SHOPGYM_SRC:-...}` so your value wins.
- **Ports.** `ss -tlnp | grep -E ':4000|:8300'` — the mock Storefront API (4000)
  and the shop (8300) live in the **host** network namespace, so a leaked
  process from a previous run can squat those ports.
- **Logs:** `journalctl -u shopgym-demo.service -f`.
- **Stop / remove:** `./deploy/teardown.sh` / `sudo ./deploy/install-service.sh --uninstall`.
