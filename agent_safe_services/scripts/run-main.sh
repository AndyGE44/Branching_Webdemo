#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
export AGENT_SAFE_SOURCE_DIR="${AGENT_SAFE_SOURCE_DIR:-/tmp/agent-safe-counter-main}"
export AGENT_SAFE_COUNTER_DB="${AGENT_SAFE_COUNTER_DB:-$AGENT_SAFE_SOURCE_DIR/state.db}"
export PYTHONPATH="$(pwd)/agent_safe_services/src${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p "$AGENT_SAFE_SOURCE_DIR"
exec .venv/bin/uvicorn agent_safe_services.counter_app:app --host 127.0.0.1 --port "${AGENT_SAFE_MAIN_PORT:-8100}"
