#!/usr/bin/env bash
# deploy/teardown.sh — stop the public demo: kill the tunnel, the control plane,
# the shop runtimes, and free the ports. Idempotent; safe to run anytime. Called
# manually or by the auto-teardown timer (see deploy/serve-public.sh).
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo ">> stopping cloudflared tunnel"
pkill -f "cloudflared tunnel" 2>/dev/null && echo "   tunnel stopped" || echo "   no tunnel running"

echo ">> stopping control plane + shop runtimes + freeing ports"
# cleanup-statefork-demo.sh kills the main port (8000) and the branch port range,
# and clears checkpoint sessions. Run it with the shopgym session dir.
CHECKPOINT_SESSIONS_DIR="${CHECKPOINT_SESSIONS_DIR:-/tmp/checkpoint-sessions-shopgym}" \
  bash "$REPO_ROOT/scripts/cleanup-statefork-demo.sh" || true

# Belt-and-suspenders: reap any leftover storefront process trees by argv marker.
for marker in "/app/run-sho[p].sh" "mock-api/mockap[i].cjs" "node serve[r].mjs"; do
  sudo pkill -9 -f "$marker" 2>/dev/null || true
done

# Cancel the auto-teardown timer if it is still scheduled.
sudo systemctl stop shopgym-demo-teardown.timer 2>/dev/null || true
sudo systemctl reset-failed shopgym-demo-teardown.service 2>/dev/null || true

echo ">> demo torn down."
