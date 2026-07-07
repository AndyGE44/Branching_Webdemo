#!/usr/bin/env bash
# deploy/install-service.sh — run the shopgym demo PERMANENTLY under systemd.
#
# Installs services that start on boot and restart on crash:
#   shopgym-demo.service         — the control plane (root; CRIU/podman need it)
#   shopgym-demo-tunnel.service  — the public tunnel (see DEMO_TUNNEL_MODE)
#
# Unlike serve-public.sh there is NO auto-teardown — it is meant to stay up. The
# control plane also auto-resets the shop after DEMO_IDLE_RESET_MINUTES of no
# activity (a shop already at its clean initial state is left alone).
#
#   sudo ./deploy/install-service.sh                       # quick tunnel (ephemeral URL)
#   DEMO_TUNNEL_MODE=named CLOUDFLARE_TUNNEL_TOKEN=eyJ... sudo -E ./deploy/install-service.sh
#   DEMO_TUNNEL_MODE=none sudo ./deploy/install-service.sh # no tunnel (front via Tailscale/direct)
#   sudo ./deploy/install-service.sh --uninstall           # stop, disable, remove
#
# DEMO_TUNNEL_MODE:
#   quick  (default) — Cloudflare quick tunnel; free, but the *.trycloudflare.com
#                      URL changes every time the tunnel restarts.
#   named            — a pre-created Cloudflare named tunnel (stable hostname).
#                      Free if you already have a domain on Cloudflare. Needs
#                      CLOUDFLARE_TUNNEL_TOKEN (Dashboard → Zero Trust → Tunnels).
#   none             — install only the control plane; expose it yourself
#                      (e.g. Tailscale Funnel for a free stable *.ts.net URL,
#                      or a firewalled direct IP:port).
#
# DEMO_TUNNEL_MODE may also be set in .env so a plain re-run keeps your choice
# (a command-line value still overrides .env). See the Tailscale Funnel recipe
# in the README for the stable free-URL setup.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CP_UNIT=shopgym-demo.service
TUN_UNIT=shopgym-demo-tunnel.service
UNIT_DIR=/etc/systemd/system
TUN_ENV=/etc/shopgym-demo-tunnel.env

say() { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
[[ "$(id -u)" -eq 0 ]] || die "run with sudo (installing systemd units needs root)."

# --- uninstall ---------------------------------------------------------------
if [[ "${1:-}" == "--uninstall" ]]; then
  say "Stopping, disabling and removing the systemd services"
  systemctl disable --now "$TUN_UNIT" 2>/dev/null || true
  systemctl disable --now "$CP_UNIT" 2>/dev/null || true
  rm -f "$UNIT_DIR/$CP_UNIT" "$UNIT_DIR/$TUN_UNIT" "$TUN_ENV"
  systemctl daemon-reload
  bash "$REPO_ROOT/deploy/teardown.sh" || true   # free ports, clear sessions
  echo ">> uninstalled."
  exit 0
fi

# Command-line overrides win over .env — capture them before .env is sourced.
_cli_tunnel_mode="${DEMO_TUNNEL_MODE:-}"
_cli_port="${DEMO_MAIN_PORT:-}"

# systemd starts services with no HOME, but run-shopgym-statefork.sh resolves the
# sibling repos via $HOME (WAYPOINT_SRC/SHOPGYM_DIR default to $HOME/Andy_* etc.)
# and runs under `set -u`. Pin HOME to the invoking user's home (the repos live
# there as siblings of this repo); fall back to the repo's parent directory.
DEMO_HOME="$(getent passwd "${SUDO_USER:-root}" 2>/dev/null | cut -d: -f6)"
[[ -n "$DEMO_HOME" && -d "$DEMO_HOME" ]] || DEMO_HOME="$(dirname "$REPO_ROOT")"

# --- .env: strong Basic Auth password + localhost bind (as serve-public.sh) ---
[[ -f .env ]] || cp .env.example .env
upsert_env() { # KEY VALUE — set or replace KEY=VALUE in .env
  if grep -qE "^${1}=" .env; then sed -i "s|^${1}=.*|${1}=${2}|" .env
  else printf '%s=%s\n' "$1" "$2" >> .env; fi
}
# shellcheck disable=SC1091
set -a; source .env; set +a
if [[ -z "${DEMO_AUTH_PASSWORD:-}" || "${DEMO_AUTH_PASSWORD}" == replace-with-a-* ]]; then
  gen="$(openssl rand -base64 18 2>/dev/null | tr -d '/+=' | cut -c1-20)"
  [[ -n "$gen" ]] || gen="demo-$(date +%s | tail -c 7)"
  upsert_env DEMO_AUTH_PASSWORD "$gen"; DEMO_AUTH_PASSWORD="$gen"
  echo "    generated a Basic Auth password (saved to .env)"
fi
DEMO_AUTH_USER="${DEMO_AUTH_USER:-demo}"
upsert_env DEMO_AUTH_USER "$DEMO_AUTH_USER"
upsert_env DEMO_MAIN_HOST 127.0.0.1   # permanent path = tunnel only; never a raw public port

# Resolve config with precedence: command-line env > .env > default. This lets
# .env pin DEMO_TUNNEL_MODE=none (e.g. when a Tailscale Funnel / self-hosted URL
# fronts the demo) so a plain re-run does NOT re-add the cloudflared tunnel,
# while `DEMO_TUNNEL_MODE=named sudo -E ...` still overrides on the command line.
PORT="${_cli_port:-${DEMO_MAIN_PORT:-8000}}"
TUNNEL_MODE="${_cli_tunnel_mode:-${DEMO_TUNNEL_MODE:-quick}}"

# --- free the decks (any manual run / old teardown timer / prior install) -----
say "Clearing any existing run"
bash "$REPO_ROOT/deploy/teardown.sh" || true

# --- resolve the tunnel command for the chosen mode --------------------------
TUN_EXEC=""
TUN_ENVLINE=""
case "$TUNNEL_MODE" in
  quick)
    if ! command -v cloudflared >/dev/null 2>&1; then
      say "Installing cloudflared"
      curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
        -o /usr/local/bin/cloudflared || die "cloudflared download failed"
      chmod +x /usr/local/bin/cloudflared
    fi
    TUN_EXEC="/usr/local/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:${PORT}"
    rm -f "$TUN_ENV"
    ;;
  named)
    [[ -n "${CLOUDFLARE_TUNNEL_TOKEN:-}" ]] || \
      die "DEMO_TUNNEL_MODE=named needs CLOUDFLARE_TUNNEL_TOKEN (Cloudflare Dashboard → Zero Trust → Tunnels)."
    command -v cloudflared >/dev/null 2>&1 || die "cloudflared is not installed."
    ( umask 077; printf 'TUNNEL_TOKEN=%s\n' "$CLOUDFLARE_TUNNEL_TOKEN" > "$TUN_ENV" )
    TUN_ENVLINE="EnvironmentFile=$TUN_ENV"   # keep the token out of the unit/argv
    TUN_EXEC="/usr/local/bin/cloudflared tunnel --no-autoupdate run"
    ;;
  none)
    rm -f "$TUN_ENV"
    ;;
  *) die "unknown DEMO_TUNNEL_MODE=$TUNNEL_MODE (expected: quick | named | none)";;
esac

# --- write the units ---------------------------------------------------------
say "Writing systemd units"
cat > "$UNIT_DIR/$CP_UNIT" <<EOF
[Unit]
Description=Shopgym StateFork demo — control plane (root; CRIU/podman)
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
WorkingDirectory=$REPO_ROOT
Environment=HOME=$DEMO_HOME
Environment=DEMO_MAIN_HOST=127.0.0.1
ExecStart=/bin/bash $REPO_ROOT/scripts/run-shopgym-statefork.sh
Restart=always
RestartSec=10
# A cold buildah/CRIU start can take a while; don't let systemd time it out.
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF

if [[ -n "$TUN_EXEC" ]]; then
  cat > "$UNIT_DIR/$TUN_UNIT" <<EOF
[Unit]
Description=Shopgym StateFork demo — public tunnel ($TUNNEL_MODE)
After=$CP_UNIT
Wants=$CP_UNIT

[Service]
Type=exec
$TUN_ENVLINE
ExecStart=$TUN_EXEC
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
else
  rm -f "$UNIT_DIR/$TUN_UNIT"
fi

# --- enable + start ----------------------------------------------------------
say "Enabling + starting services (start on boot, restart on crash)"
systemctl daemon-reload
systemctl enable --now "$CP_UNIT"
if [[ -n "$TUN_EXEC" ]]; then
  systemctl enable --now "$TUN_UNIT"
else
  systemctl disable --now "$TUN_UNIT" 2>/dev/null || true
fi

# --- wait for readiness ------------------------------------------------------
echo -n "    waiting for the control plane"
ready=""
for _ in $(seq 1 120); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/healthz" 2>/dev/null || echo 000)"
  if [[ "$code" == "200" ]]; then ready=1; echo " ready"; break; fi
  echo -n "."; sleep 2
done
[[ -n "$ready" ]] || echo " (still starting — watch: journalctl -u $CP_UNIT -f)"

# --- capture the quick-tunnel URL from the journal ---------------------------
url=""
if [[ "$TUNNEL_MODE" == quick ]]; then
  for _ in $(seq 1 40); do
    url="$(journalctl -u "$TUN_UNIT" --no-pager 2>/dev/null \
            | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 || true)"
    [[ -n "$url" ]] && break; sleep 1
  done
fi

# --- summary -----------------------------------------------------------------
cat <<EOF

  ┌────────────────────────────────────────────────────────────
  │  Permanent demo installed — starts on boot, restarts on crash.
  │  Control plane : http://127.0.0.1:${PORT}   (root, behind Basic Auth)
  │  Login         : ${DEMO_AUTH_USER} / ${DEMO_AUTH_PASSWORD}
EOF
case "$TUNNEL_MODE" in
  quick) echo "  │  Public URL    : ${url:-"(starting — journalctl -u $TUN_UNIT | grep trycloudflare)"}"
         echo "  │  NOTE          : quick-tunnel URL CHANGES on every tunnel restart" ;;
  named) echo "  │  Public URL    : your named-tunnel hostname (set in the Cloudflare dashboard)" ;;
  none)  echo "  │  Public URL    : no tunnel installed — expose 127.0.0.1:${PORT} yourself" ;;
esac
cat <<EOF
  │  Idle reset    : after ${DEMO_IDLE_RESET_MINUTES:-10} min idle (skipped when already clean)
  │  Status        : systemctl status $CP_UNIT ${TUN_EXEC:+$TUN_UNIT}
  │  Logs          : journalctl -u $CP_UNIT -f
  │  Stop now      : ./deploy/teardown.sh        (services still restart on boot)
  │  Remove all    : sudo ./deploy/install-service.sh --uninstall
  └────────────────────────────────────────────────────────────

EOF
