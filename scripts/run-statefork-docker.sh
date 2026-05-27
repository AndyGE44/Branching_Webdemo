#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export DEMO_BRANCH_BACKEND="${DEMO_BRANCH_BACKEND:-statefork}"
export DEMO_STATEFORK_BUILD=1
export DEMO_STATEFORK_ROOT="${DEMO_STATEFORK_ROOT:-/users/alexxjk/StateFork}"
export DEMO_STATEFORK_CWD="${DEMO_STATEFORK_CWD:-/users/alexxjk/StateFork}"
export DEMO_STATEFORK_METHOD="${DEMO_STATEFORK_METHOD:-ckpt_build}"
export CHECKPOINT_SESSIONS_DIR="${CHECKPOINT_SESSIONS_DIR:-/tmp/checkpoint-sessions-mailbox-demo}"
export DEMO_BRANCH_HOST="${DEMO_BRANCH_HOST:-127.0.0.1}"
export DEMO_BRANCH_PORT_START="${DEMO_BRANCH_PORT_START:-8300}"
export DEMO_MAILBOX_DB_PATH="${DEMO_MAILBOX_DB_PATH:-${PWD}/demo_mailbox.db}"
export PYTHONPATH="${PWD}/src${PYTHONPATH:+:${PYTHONPATH}}"

host="${DEMO_MAIN_HOST:-127.0.0.1}"
port="${DEMO_MAIN_PORT:-8000}"

if ! command -v sudo >/dev/null 2>&1; then
  echo "sudo is required for StateFork/checkpoint-lite demo mode." >&2
  exit 1
fi

if [[ ! -x .venv/bin/uvicorn ]]; then
  echo "Missing .venv/bin/uvicorn. Run: python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'" >&2
  exit 1
fi

if [[ ! -f Dockerfile ]]; then
  echo "Missing Dockerfile in $(pwd)." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required for DEMO_STATEFORK_BUILD=1. Install docker.io on the VM first." >&2
  exit 1
fi

if sudo lsof -tiTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port ${port} is already in use. Stop the old demo first:" >&2
  echo "  ./scripts/cleanup-statefork-demo.sh" >&2
  exit 1
fi

cat <<EOF
Starting Agent-Safe Mailbox with StateFork Docker build mode.

Main controller: http://${host}:${port}
Runtime ports:   ${DEMO_BRANCH_HOST}:${DEMO_BRANCH_PORT_START}+
Sessions dir:    ${CHECKPOINT_SESSIONS_DIR}
Mailbox DB:      ${DEMO_MAILBOX_DB_PATH}

The UI should show: statefork / statefork:ckpt_build / Docker build
EOF

exec sudo -E .venv/bin/uvicorn agent_safe_demo.main:app --host "$host" --port "$port"
