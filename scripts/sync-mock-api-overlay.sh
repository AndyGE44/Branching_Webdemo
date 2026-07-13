#!/usr/bin/env bash
# sync-mock-api-overlay.sh — copy the canonical mock-api Dolt overlay source into
# each shop build dir so the shop Dockerfiles' `COPY mock-api-overlay/` works.
#
# The control plane also does this at startup (control_plane/overlay_sync.py);
# run this manually after editing app_plane/mock_api_overlay/, or in a build
# pipeline that builds shop images without starting the control plane.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_PLANE="$ROOT/src/agent_safe_demo/app_plane"
CANONICAL="$APP_PLANE/mock_api_overlay"

if [ ! -d "$CANONICAL" ]; then
  echo "canonical overlay dir not found: $CANONICAL" >&2
  exit 1
fi

count=0
for shop in "$APP_PLANE"/shop_*/; do
  [ -f "${shop}Dockerfile" ] || continue
  dest="${shop}mock-api-overlay"
  mkdir -p "$dest"
  cp -f "$CANONICAL"/*.ts "$dest"/
  echo "synced overlay -> $dest"
  count=$((count + 1))
done
echo "done: updated $count shop build dirs"
