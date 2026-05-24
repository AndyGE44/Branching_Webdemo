#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

host="${TOY_MAIN_HOST:-127.0.0.1}"
port="${TOY_MAIN_PORT:-8000}"

exec cloudflared tunnel --url "http://${host}:${port}"
