#!/usr/bin/env bash
# run-shopgym-statefork.sh — QUICK-TEST launcher for the shopgym StateFork demo.
#
#   ***  For anything other than a local quick test, use the recommended    ***
#   ***  path: ./deploy/serve-public.sh (localhost bind + HTTPS tunnel +    ***
#   ***  Basic Auth + auto-teardown). The control plane runs as ROOT for   ***
#   ***  CRIU/podman and must never face the network unauthenticated.      ***
#
# This script binds 127.0.0.1 only. It satisfies the host prerequisites the
# shop containers need to be CRIU-checkpointable (these are NOT things the
# webdemo repo can carry), then launches the control plane in StateFork build
# mode. Select a shop in the UI.
#
#   1. kernel.io_uring_disabled=2  — Node 22 libuv uses io_uring, which CRIU
#      cannot checkpoint.
#   2. shop images in *root* container storage — Waypoint builds with `buildah`
#      as root, so the `FROM localhost/shop-arena-mock-*` base must live there.
#   3. Waypoint built with the Node-friendly CRIU flags (--force-irmap
#      --link-remap) — without them CRIU can't dump the shop's inotify watches.
#
# Configuration comes from .env (copy .env.example) or the matching env vars
# (DEMO_STATEFORK_ROOT, WAYPOINT_SRC, SHOPGYM_DIR, DEMO_MAIN_PORT, ...);
# explicit environment variables win over .env.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO_ROOT="$PWD"

# --- configuration: .env provides defaults, explicit env vars win -------------
if [[ -f .env ]]; then
  _pre_env="$(env | grep -E '^(DEMO_|WAYPOINT_|SHOPGYM_|CHECKPOINT_)[A-Za-z0-9_]*=' || true)"
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
  while IFS='=' read -r _key _value; do
    [[ -n "$_key" ]] || continue
    export "$_key=$_value"
  done <<<"$_pre_env"
  unset _key _value _pre_env
fi

STATEFORK_ROOT="${DEMO_STATEFORK_ROOT:-$HOME/Andy_StateFork}"
WAYPOINT_SRC="${WAYPOINT_SRC:-$HOME/Andy_Waypoint}"
SHOPGYM_DIR="${SHOPGYM_DIR:-$HOME/shopgym}"
# Quick-test default: localhost only. Overriding to a public interface requires
# Basic Auth (or an explicit unsafe opt-in) — see the guard below.
HOST="${DEMO_MAIN_HOST:-127.0.0.1}"
PORT="${DEMO_MAIN_PORT:-8000}"

if [[ "$HOST" != "127.0.0.1" && "$HOST" != "localhost" && "$HOST" != "::1" ]]; then
  if [[ -z "${DEMO_AUTH_PASSWORD:-}" && "${DEMO_ALLOW_UNAUTH_PUBLIC:-0}" != "1" ]]; then
    cat >&2 <<'EOF'
ERROR: refusing to bind a non-localhost interface without Basic Auth.
       The control plane runs as root — never expose it unauthenticated.

       Recommended:  ./deploy/serve-public.sh   (tunnel + auth + auto-teardown)
       Alternatives: set DEMO_AUTH_PASSWORD in .env, or force with
       DEMO_ALLOW_UNAUTH_PUBLIC=1 (only behind a host firewall allowlist).
EOF
    exit 1
  fi
  echo "WARNING: binding ${HOST}:${PORT} — the demo is reachable from the network." >&2
fi

# Shop container images to make available (also covers cookware/hardware).
SHOP_IMAGES=(
  "shop-arena-mock-clothing"
  "shop-arena-mock-cookware"
  "shop-arena-mock-hardware"
)

command -v sudo >/dev/null 2>&1 || { echo "sudo is required (CRIU needs root)." >&2; exit 1; }
[[ -x "$REPO_ROOT/.venv/bin/uvicorn" ]] || {
  echo "Missing .venv/bin/uvicorn. Create it: python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'" >&2
  exit 1
}

# Values needed both by the clean-slate step and the launch step.
CHECKPOINT_SESSIONS_DIR="${CHECKPOINT_SESSIONS_DIR:-/tmp/checkpoint-sessions-shopgym}"
WAYPOINT_SESSIONS_DIR="${WAYPOINT_SESSIONS_DIR:-$(
  python3 - <<'PY' 2>/dev/null || true
import json
try:
    print(json.load(open("/etc/waypoint/config.json")).get("sessions_dir", ""))
except Exception:
    pass
PY
)}"
WAYPOINT_SESSIONS_DIR="${WAYPOINT_SESSIONS_DIR:-/mydata/waypoint-sessions}"
BRANCH_PORT_START="${DEMO_BRANCH_PORT_START:-8300}"
BRANCH_PORT_END="${DEMO_BRANCH_PORT_END:-8350}"

echo ">> 0/4  Clean slate (kill leftovers from a previous run)"
# 1. Kill any storefront process tree left running from a previous launch. These
#    orphaned mock-api/Hydrogen processes keep serving a STALE in-memory cart, so
#    a "fresh" website would otherwise show items already in the cart. Match by
#    SPECIFIC argv markers via pkill (which excludes its own pid); these patterns
#    only hit the shop runtime — NOT a blanket `pkill node`, which would also kill
#    the IDE's node processes on this VM.
# The bracket around one char (e.g. mockap[i]) is the classic `grep '[p]attern'`
# trick: the regex still matches the real process cmdline, but the pattern text
# itself does NOT contain the literal marker, so pkill won't match its own
# argv or the parent `sudo` and kill them mid-cleanup.
killed_any=0
for marker in "/app/run-sho[p].sh" "mock-api/mockap[i].cjs" "node serve[r].mjs"; do
  if sudo pkill -9 -f "$marker" 2>/dev/null; then
    echo "        killed leftover storefront process(es) matching: ${marker//[\[\]]/}"
    killed_any=1
  fi
done
[[ "$killed_any" -eq 1 ]] || echo "        no leftover storefront processes"

# 2. Free the control-plane port and the branch port range, in case a previous
#    server or branch runtime is still listening.
for port in "$PORT" $(seq "$BRANCH_PORT_START" "$BRANCH_PORT_END"); do
  pids="$(sudo lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    echo "        freeing port $port: $pids"
    # shellcheck disable=SC2086
    sudo kill -9 $pids 2>/dev/null || true
  fi
done

# 3. Wipe on-disk checkpoint/restore session state. A new run must BUILD a fresh
#    container; reusing a stale waypoint session restores a container whose
#    mock-api memory still holds an old cart (the "RAM leak" symptom).
for dir in "$CHECKPOINT_SESSIONS_DIR" "$WAYPOINT_SESSIONS_DIR"; do
  if [[ -n "$dir" && -d "$dir" ]]; then
    echo "        clearing sessions: $dir"
    for t in $(findmnt -R -n -o TARGET "$dir" 2>/dev/null | sort -r); do
      sudo umount -l "$t" 2>/dev/null || true
    done
    sudo rm -rf "${dir:?}/"* 2>/dev/null || true
  fi
done

echo ">> 1/4  Disable io_uring (CRIU 4.x cannot checkpoint it)"
if [[ "$(sudo sysctl -n kernel.io_uring_disabled 2>/dev/null || echo 0)" != "2" ]]; then
  sudo sysctl -w kernel.io_uring_disabled=2
else
  echo "        already disabled"
fi

echo ">> 2/4  Ensure shop images in root container storage"
for img in "${SHOP_IMAGES[@]}"; do
  if sudo podman image exists "localhost/$img:latest" 2>/dev/null; then
    echo "        ok: $img"
  elif [[ -f "$SHOPGYM_DIR/docker-images/$img.tar.gz" ]]; then
    echo "        loading: $img"
    sudo podman load -i "$SHOPGYM_DIR/docker-images/$img.tar.gz" >/dev/null
  else
    echo "        WARN: $img not in root storage and $SHOPGYM_DIR/docker-images/$img.tar.gz is missing" >&2
  fi
done

echo ">> 3/4  Ensure Waypoint (+ bash_init) is built with Node-friendly CRIU flags"
mem_go="$WAYPOINT_SRC/pkg/waypoint/memory.go"
wp_bin="$WAYPOINT_SRC/waypoint"
bi_bin="$WAYPOINT_SRC/bash_init"
if ! grep -q -- "--force-irmap" "$mem_go" 2>/dev/null; then
  echo "        ERROR: $mem_go is missing --force-irmap; shop checkpoints will fail." >&2
  echo "        Add '--force-irmap' and '--link-remap' to the criu dump args, then rerun." >&2
  exit 1
fi
if [[ ! -x "$wp_bin" || "$mem_go" -nt "$wp_bin" ]]; then
  echo "        building waypoint"
  PATH="$PATH:/usr/local/go/bin" go -C "$WAYPOINT_SRC" build -o waypoint ./cmd/waypoint
else
  echo "        ok: waypoint up to date"
fi
# bash_init is the chroot-embedded managed shell Waypoint launches inside each
# built container; without it `waypoint build` cannot start the app.
if [[ ! -x "$bi_bin" ]]; then
  echo "        building bash_init"
  PATH="$PATH:/usr/local/go/bin" go -C "$WAYPOINT_SRC" build -o bash_init ./cmd/bash-init
else
  echo "        ok: bash_init present"
fi

echo ">> 4/4  Launch control plane (StateFork build mode)"
export DEMO_STATEFORK_BUILD=1
export DEMO_STATEFORK_ROOT="$STATEFORK_ROOT"
export DEMO_STATEFORK_CWD="${DEMO_STATEFORK_CWD:-$STATEFORK_ROOT}"
export DEMO_STATEFORK_METHOD="${DEMO_STATEFORK_METHOD:-ckpt_build}"
# Point StateFork's waypoint backend at the binary we just built (it resolves
# WAYPOINT_BIN, then $PATH, then ./waypoint), and Waypoint at its bash_init helper.
export WAYPOINT_BIN="${WAYPOINT_BIN:-$wp_bin}"
export WAYPOINT_BASH_INIT_SRC="${WAYPOINT_BASH_INIT_SRC:-$bi_bin}"
export CHECKPOINT_SESSIONS_DIR
export DEMO_BRANCH_HOST="${DEMO_BRANCH_HOST:-127.0.0.1}"
export DEMO_BRANCH_PORT_START="${DEMO_BRANCH_PORT_START:-8300}"
export DEMO_APP_ID="${DEMO_APP_ID:-shop_clothing}"
# Restrict the App selector to the three shops.
export DEMO_VISIBLE_APP_IDS="${DEMO_VISIBLE_APP_IDS:-shop_clothing,shop_cookware,shop_hardware}"
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

# When bound to all interfaces, show a reachable address (LAN IP, falling back to
# localhost) instead of the unhelpful 0.0.0.0 in the URL.
if [[ "$HOST" == "0.0.0.0" || "$HOST" == "::" ]]; then
  DISPLAY_HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
  DISPLAY_HOST="${DISPLAY_HOST:-127.0.0.1}"
else
  DISPLAY_HOST="$HOST"
fi

cat <<EOF

  Control plane:  http://${DISPLAY_HOST}:${PORT}   (bound on ${HOST})
  Default app:    ${DEMO_APP_ID}   (switch shops in the UI)
  Branch ports:   ${DEMO_BRANCH_HOST}:${DEMO_BRANCH_PORT_START}+
  Public demo:    use ./deploy/serve-public.sh instead (tunnel + auth + TTL)

EOF

exec sudo -E "$REPO_ROOT/.venv/bin/uvicorn" agent_safe_demo.control_plane.main:app --host "$HOST" --port "$PORT"
