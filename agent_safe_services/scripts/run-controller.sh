#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
export AGENT_SAFE_SOURCE_DIR="${AGENT_SAFE_SOURCE_DIR:-/tmp/agent-safe-counter-main}"
export AGENT_SAFE_CKPT_BIN="${AGENT_SAFE_CKPT_BIN:-/users/alexxjk/checkpoint-lite/checkpoint-lite}"
export AGENT_SAFE_SESSIONS_DIR="${AGENT_SAFE_SESSIONS_DIR:-/tmp/checkpoint-sessions-agent-safe-services}"
export AGENT_SAFE_SESSION_INFO_DIR="${AGENT_SAFE_SESSION_INFO_DIR:-/tmp/checkpoint-sessions-info-agent-safe-services}"
export AGENT_SAFE_USE_SUDO="${AGENT_SAFE_USE_SUDO:-1}"
export AGENT_SAFE_BRANCH_HOST="${AGENT_SAFE_BRANCH_HOST:-127.0.0.1}"
export AGENT_SAFE_BRANCH_PORT_START="${AGENT_SAFE_BRANCH_PORT_START:-8400}"
export AGENT_SAFE_PUBLIC_BASE_URL="${AGENT_SAFE_PUBLIC_BASE_URL:-http://127.0.0.1:8000}"
export PYTHONPATH="$(pwd)/agent_safe_services/src${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p "$AGENT_SAFE_SOURCE_DIR"
exec .venv/bin/uvicorn agent_safe_services.controller_app:app --host 127.0.0.1 --port "${AGENT_SAFE_CONTROLLER_PORT:-8000}"
