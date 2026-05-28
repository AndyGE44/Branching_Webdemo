#!/usr/bin/env bash
set -euo pipefail

SESSIONS_DIR="${AGENT_SAFE_SESSIONS_DIR:-/tmp/checkpoint-sessions-agent-safe-services}"
INFO_DIR="${AGENT_SAFE_SESSION_INFO_DIR:-/tmp/checkpoint-sessions-info-agent-safe-services}"
SOURCE_DIR="${AGENT_SAFE_SOURCE_DIR:-/tmp/agent-safe-counter-main}"

pkill -f "agent_safe_services.controller_app:app" 2>/dev/null || true
pkill -f "agent_safe_services.counter_app:app" 2>/dev/null || true
sleep 0.5

if command -v findmnt >/dev/null 2>&1 && [ -e "$SESSIONS_DIR" ]; then
  mapfile -t mounts < <(findmnt -R "$SESSIONS_DIR" -n -o TARGET 2>/dev/null | sort -r)
  for mountpoint in "${mounts[@]}"; do
    sudo umount -f -l "$mountpoint" 2>/dev/null || true
  done
else
  sudo umount -f -l "$SESSIONS_DIR"/*/work 2>/dev/null || true
fi

sudo rm -rf "$SESSIONS_DIR" "$INFO_DIR"
rm -rf "$SOURCE_DIR"
