#!/usr/bin/env bash
set -euo pipefail
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared is not installed. Install it first, then rerun this script." >&2
  exit 1
fi
exec cloudflared tunnel --url "http://127.0.0.1:${AGENT_SAFE_CONTROLLER_PORT:-8000}"
