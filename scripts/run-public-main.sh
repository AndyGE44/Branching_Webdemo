#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

: "${TOY_DEMO_AUTH_PASSWORD:?Set TOY_DEMO_AUTH_PASSWORD in .env before starting a public demo}"

export TOY_DEMO_AUTH_USER="${TOY_DEMO_AUTH_USER:-demo}"
export TOY_BRANCH_BACKEND="${TOY_BRANCH_BACKEND:-statefork}"
export TOY_STATEFORK_ROOT="${TOY_STATEFORK_ROOT:-/users/alexxjk/StateFork}"
export TOY_STATEFORK_CWD="${TOY_STATEFORK_CWD:-/users/alexxjk/StateFork}"
export TOY_STATEFORK_METHOD="${TOY_STATEFORK_METHOD:-ckpt_build}"
export CHECKPOINT_SESSIONS_DIR="${CHECKPOINT_SESSIONS_DIR:-/tmp/checkpoint-sessions}"
export TOY_BRANCH_HOST="${TOY_BRANCH_HOST:-127.0.0.1}"
export TOY_BRANCH_PORT_START="${TOY_BRANCH_PORT_START:-8300}"
export PYTHONPATH="${PYTHONPATH:-src}"

host="${TOY_MAIN_HOST:-127.0.0.1}"
port="${TOY_MAIN_PORT:-8000}"

exec sudo -E .venv/bin/uvicorn agent_safe_demo.main:app --host "$host" --port "$port"
