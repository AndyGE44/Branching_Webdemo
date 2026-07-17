#!/usr/bin/env bash
# deploy/status.sh — is a demo live, and what's its URL + login?
#
# Reprints the access box for a running demo, however it was started
# (serve-public.sh quick tunnel, systemd services, a Tailscale Funnel, or a
# localhost-only quick test). Read-only and idempotent — safe to run anytime.
#
#   ./deploy/status.sh
#
# Exit status: 0 if the control plane is healthy, 1 if it is down — so scripts
# can gate on `./deploy/status.sh >/dev/null`.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
RUN_DIR="${DEMO_RUN_DIR:-/tmp/shopgym-demo-run}"

# --- .env for host/port/login defaults (explicit env still wins) --------------
# Snapshot explicit env first so it overrides .env, matching the launchers.
_pre="$(env | grep -E '^DEMO_(MAIN_HOST|MAIN_PORT|AUTH_USER|AUTH_PASSWORD)=' || true)"
if [[ -f .env ]]; then set -a; . ./.env; set +a; fi
while IFS='=' read -r k v; do [[ -n "$k" ]] && export "$k=$v"; done <<<"$_pre"
PORT="${DEMO_MAIN_PORT:-8000}"
AUTH_USER="${DEMO_AUTH_USER:-demo}"
AUTH_PASS="${DEMO_AUTH_PASSWORD:-}"

probe() { # url -> HTTP code (000 if unreachable)
  curl -s -o /dev/null -w '%{http_code}' --max-time 6 "$1" 2>/dev/null || echo 000
}

# --- is the control plane up? -------------------------------------------------
health="$(probe "http://127.0.0.1:${PORT}/healthz")"
[[ "$health" == "200" ]] && up=1 || up=0

# --- find the public URL (each start mode stashes it differently) -------------
url=""; url_src=""
# 1. serve-public.sh quick tunnel — URL logged by cloudflared.
if [[ -z "$url" && -f "$RUN_DIR/tunnel.log" ]] && pgrep -f "cloudflared tunnel" >/dev/null 2>&1; then
  url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$RUN_DIR/tunnel.log" | tail -1 || true)"
  [[ -n "$url" ]] && url_src="cloudflare quick tunnel"
fi
# 2. systemd tunnel service (install-service.sh, quick mode) — URL in its journal.
if [[ -z "$url" ]] && systemctl is-active --quiet shopgym-demo-tunnel.service 2>/dev/null; then
  url="$(journalctl -u shopgym-demo-tunnel.service 2>/dev/null \
        | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 || true)"
  [[ -n "$url" ]] && url_src="cloudflare tunnel (systemd)"
fi
# 3. Tailscale Funnel (none mode / EC2) — stable *.ts.net.
if [[ -z "$url" ]] && command -v tailscale >/dev/null 2>&1; then
  url="$(tailscale funnel status 2>/dev/null | grep -oE 'https://[A-Za-z0-9.-]+\.ts\.net' | head -1 || true)"
  [[ -n "$url" ]] && url_src="tailscale funnel"
fi

# Confirm the URL is actually current (quick-tunnel URLs change every restart):
# our app answers 401 (auth on) or 200 (auth off); a stale/dead tunnel gives 000/5xx.
url_state=""
if [[ -n "$url" ]]; then
  code="$(probe "$url/healthz")"
  case "$code" in
    200|401) url_state="reachable (HTTP $code)";;
    000)     url_state="NOT reachable — likely stale/torn down";;
    *)       url_state="unexpected HTTP $code";;
  esac
fi

# --- auto-teardown timer (serve-public.sh only) -------------------------------
teardown=""
if systemctl is-active --quiet shopgym-demo-teardown.timer 2>/dev/null; then
  # list-timers row: NEXT(date time tz)=$1..$4, LEFT=$5.
  teardown="$(systemctl list-timers shopgym-demo-teardown.timer --no-pager 2>/dev/null \
             | awk 'NR==2{printf "%s %s %s %s (in %s)", $1,$2,$3,$4,$5}')"
fi
# Is the run supervised by systemd (survives reboots) or a foreground script run?
mode="foreground (serve-public.sh / quick test)"
systemctl is-enabled --quiet shopgym-demo.service 2>/dev/null && mode="systemd service (boot-persistent)"

# --- render -------------------------------------------------------------------
line() { printf '  │  %s\n' "$*"; }
printf '  ┌%s\n' "────────────────────────────────────────────────────────────"
if [[ "$up" == "1" ]]; then
  line "Demo         : LIVE  (control plane healthy on 127.0.0.1:${PORT})"
  line "Run mode     : ${mode}"
  if [[ -n "$url" ]]; then
    line "Public URL   : ${url}"
    line "               via ${url_src} — ${url_state}"
  else
    line "Public URL   : (none) — localhost:${PORT} only (SSH-tunnel to reach it)"
  fi
  if [[ -n "$AUTH_PASS" ]]; then
    line "Login        : ${AUTH_USER} / ${AUTH_PASS}"
  else
    line "Login        : (auth OFF — no DEMO_AUTH_PASSWORD in .env)"
  fi
  [[ -n "$teardown" ]] && line "Auto-teardown: ${teardown}"
  line "Stop now     : ./deploy/teardown.sh"
else
  line "Demo         : DOWN  (nothing healthy on 127.0.0.1:${PORT})"
  [[ -n "$url" ]] && line "Last URL seen: ${url}  (${url_state})"
  line "Start it     : ./deploy/serve-public.sh   (public: tunnel + auth + TTL)"
fi
printf '  └'; printf '%s\n' "────────────────────────────────────────────────────────────"

[[ "$up" == "1" ]]
