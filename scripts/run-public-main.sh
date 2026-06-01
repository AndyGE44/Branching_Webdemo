#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

: "${DEMO_AUTH_PASSWORD:?Set DEMO_AUTH_PASSWORD in .env before starting a public demo}"

export DEMO_AUTH_USER="${DEMO_AUTH_USER:-demo}"
export DEMO_STATEFORK_ROOT="${DEMO_STATEFORK_ROOT:-/users/alexxjk/StateFork}"
export DEMO_STATEFORK_CWD="${DEMO_STATEFORK_CWD:-/users/alexxjk/StateFork}"
export DEMO_STATEFORK_METHOD="${DEMO_STATEFORK_METHOD:-ckpt_build}"
export CHECKPOINT_SESSIONS_DIR="${CHECKPOINT_SESSIONS_DIR:-/tmp/checkpoint-sessions}"
export DEMO_BRANCH_HOST="${DEMO_BRANCH_HOST:-127.0.0.1}"
export DEMO_BRANCH_PORT_START="${DEMO_BRANCH_PORT_START:-8300}"
export DEMO_APP_ID="${DEMO_APP_ID:-email}"
export DEMO_MAILBOX_DB_PATH="${DEMO_MAILBOX_DB_PATH:-${PWD}/demo_mailbox.db}"
export DEMO_INVENTORY_DB_PATH="${DEMO_INVENTORY_DB_PATH:-${PWD}/demo_inventory.db}"
export PYTHONPATH="${PYTHONPATH:-src}"

host="${DEMO_MAIN_HOST:-127.0.0.1}"
port="${DEMO_MAIN_PORT:-8000}"

exec sudo -E .venv/bin/uvicorn agent_safe_demo.control_plane.main:app --host "$host" --port "$port"
