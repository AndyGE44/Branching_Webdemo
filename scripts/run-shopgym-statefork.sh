#!/usr/bin/env bash
# run-shopgym-statefork.sh — one command to run the StateFork control plane with
# the shopgym (Shopify Hydrogen mock) shops as branchable apps.
#
# It first satisfies the host prerequisites that the shop containers need to be
# CRIU-checkpointable (these are NOT things the webdemo repo can carry), then
# launches the control plane in StateFork build mode. Select a shop in the UI.
#
#   1. kernel.io_uring_disabled=2  — Node 22 libuv uses io_uring, which CRIU
#      cannot checkpoint.
#   2. shop images in *root* container storage — Waypoint builds with `buildah`
#      as root, so the `FROM localhost/shop-arena-mock-*` base must live there.
#   3. Waypoint built with the Node-friendly CRIU flags (--force-irmap
#      --link-remap) — without them CRIU can't dump the shop's inotify watches.
#
# Override any path with the matching env var (DEMO_STATEFORK_ROOT, WAYPOINT_SRC,
# SHOPGYM_DIR, DEMO_MAIN_PORT, DEMO_APP_ID, ...).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO_ROOT="$PWD"

STATEFORK_ROOT="${DEMO_STATEFORK_ROOT:-$HOME/Andy_StateFork}"
WAYPOINT_SRC="${WAYPOINT_SRC:-$HOME/Andy_Waypoint}"
SHOPGYM_DIR="${SHOPGYM_DIR:-$HOME/shopgym}"
HOST="${DEMO_MAIN_HOST:-127.0.0.1}"
PORT="${DEMO_MAIN_PORT:-8000}"

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
export CHECKPOINT_SESSIONS_DIR="${CHECKPOINT_SESSIONS_DIR:-/tmp/checkpoint-sessions-shopgym}"
export DEMO_BRANCH_HOST="${DEMO_BRANCH_HOST:-127.0.0.1}"
export DEMO_BRANCH_PORT_START="${DEMO_BRANCH_PORT_START:-8300}"
export DEMO_APP_ID="${DEMO_APP_ID:-shop_clothing}"
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

cat <<EOF

  Control plane:  http://${HOST}:${PORT}
  Default app:    ${DEMO_APP_ID}   (switch shops in the UI)
  Branch ports:   ${DEMO_BRANCH_HOST}:${DEMO_BRANCH_PORT_START}+

EOF

exec sudo -E "$REPO_ROOT/.venv/bin/uvicorn" agent_safe_demo.control_plane.main:app --host "$HOST" --port "$PORT"
