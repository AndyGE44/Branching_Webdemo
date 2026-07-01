#!/usr/bin/env bash
# deploy/serve-public.sh — expose the shopgym demo safely for a timed public run.
#
# The control plane runs as root, so this never opens a public port: it binds the
# app to localhost and reaches the outside world through a Cloudflare quick tunnel
# (HTTPS), behind Basic Auth, and schedules an automatic teardown.
#
#   ./deploy/serve-public.sh            # start; prints the https URL + login
#   DEMO_TTL_HOURS=8 ./deploy/serve-public.sh
#   ./deploy/teardown.sh                # stop everything now (or wait for the timer)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
RUN_DIR="${DEMO_RUN_DIR:-/tmp/shopgym-demo-run}"
mkdir -p "$RUN_DIR"

say() { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# --- .env: ensure a real Basic Auth password and localhost binding ------------
[[ -f .env ]] || cp .env.example .env
upsert_env() { # KEY VALUE  — set or replace KEY=VALUE in .env
  if grep -qE "^${1}=" .env; then
    sed -i "s|^${1}=.*|${1}=${2}|" .env
  else
    printf '%s=%s\n' "$1" "$2" >> .env
  fi
}
# shellcheck disable=SC1091
set -a; source .env; set +a

if [[ -z "${DEMO_AUTH_PASSWORD:-}" || "${DEMO_AUTH_PASSWORD}" == "replace-with-a-strong-password" || "${DEMO_AUTH_PASSWORD}" == "replace-with-a-demo-password" ]]; then
  gen="$(openssl rand -base64 18 2>/dev/null | tr -d '/+=' | cut -c1-20)"
  [[ -n "$gen" ]] || gen="demo-$(date +%s | tail -c 7)"
  upsert_env DEMO_AUTH_PASSWORD "$gen"
  DEMO_AUTH_PASSWORD="$gen"
  echo "    generated a Basic Auth password (saved to .env)"
fi
DEMO_AUTH_USER="${DEMO_AUTH_USER:-demo}"
upsert_env DEMO_AUTH_USER "$DEMO_AUTH_USER"
# Public path = tunnel only. Force localhost so the raw port is never exposed —
# both in .env and in this process's environment (the launcher honors either).
upsert_env DEMO_MAIN_HOST 127.0.0.1
export DEMO_MAIN_HOST=127.0.0.1 DEMO_AUTH_USER DEMO_AUTH_PASSWORD

# --- cloudflared --------------------------------------------------------------
if ! command -v cloudflared >/dev/null 2>&1; then
  say "Installing cloudflared"
  sudo curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
    -o /usr/local/bin/cloudflared || die "cloudflared download failed"
  sudo chmod +x /usr/local/bin/cloudflared
fi

# --- start the control plane (localhost) --------------------------------------
say "Starting control plane on 127.0.0.1:${DEMO_MAIN_PORT:-8000} (root, behind auth)"
nohup bash "$REPO_ROOT/scripts/run-shopgym-statefork.sh" >"$RUN_DIR/control-plane.log" 2>&1 &
echo $! > "$RUN_DIR/control-plane.pid"

port="${DEMO_MAIN_PORT:-8000}"
echo -n "    waiting for the app to come up"
ready=""
for _ in $(seq 1 120); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/" 2>/dev/null || echo 000)"
  if [[ "$code" != "000" ]]; then ready="$code"; echo " ready (HTTP $code)"; break; fi
  echo -n "."; sleep 2
done
[[ -n "$ready" ]] || die "control plane did not come up — see $RUN_DIR/control-plane.log"

# --- start the tunnel + capture the URL ---------------------------------------
say "Opening Cloudflare quick tunnel"
nohup cloudflared tunnel --url "http://127.0.0.1:${port}" >"$RUN_DIR/tunnel.log" 2>&1 &
echo $! > "$RUN_DIR/tunnel.pid"
url=""
for _ in $(seq 1 40); do
  url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$RUN_DIR/tunnel.log" | head -1 || true)"
  [[ -n "$url" ]] && break
  sleep 1
done

# --- schedule auto-teardown ---------------------------------------------------
ttl="${DEMO_TTL_HOURS:-24}"
sudo systemctl reset-failed shopgym-demo-teardown.service shopgym-demo-teardown.timer 2>/dev/null || true
if sudo systemd-run --on-active="${ttl}h" --unit=shopgym-demo-teardown --collect \
     /bin/bash "$REPO_ROOT/deploy/teardown.sh" >/dev/null 2>&1; then
  teardown_note="auto-teardown in ${ttl}h (cancel: sudo systemctl stop shopgym-demo-teardown.timer)"
else
  nohup bash -c "sleep $((ttl*3600)); bash '$REPO_ROOT/deploy/teardown.sh'" >/dev/null 2>&1 &
  echo $! > "$RUN_DIR/teardown.pid"
  teardown_note="auto-teardown in ${ttl}h (fallback timer pid $(cat "$RUN_DIR/teardown.pid"))"
fi

# --- summary ------------------------------------------------------------------
cat <<EOF

  ┌────────────────────────────────────────────────────────────
  │  Public URL : ${url:-"(not captured — see $RUN_DIR/tunnel.log)"}
  │  Login      : ${DEMO_AUTH_USER} / ${DEMO_AUTH_PASSWORD}
  │  ${teardown_note}
  │  Stop now   : ./deploy/teardown.sh
  │  Logs       : $RUN_DIR/{control-plane,tunnel}.log
  └────────────────────────────────────────────────────────────

EOF
