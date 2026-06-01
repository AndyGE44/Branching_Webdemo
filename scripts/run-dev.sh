#!/usr/bin/env bash
set -euo pipefail

export PYTHONPATH="${PWD}/src${PYTHONPATH:+:${PYTHONPATH}}"
uvicorn agent_safe_demo.control_plane.main:app --reload --reload-dir src --host 127.0.0.1 --port 8000
