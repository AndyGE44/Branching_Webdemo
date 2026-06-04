#!/usr/bin/env bash
set -euo pipefail

host="127.0.0.1"
port="${PORT:-8300}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "${script_dir}/../../../.." && pwd)"
python_bin="${project_root}/.venv/bin/python"
if [[ ! -x "${python_bin}" ]]; then
  python_bin="python"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      host="$2"
      shift 2
      ;;
    --port)
      port="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

exec "${python_bin}" -m uvicorn agent_safe_demo.app_plane.kv_service.app:app --host "$host" --port "$port"
