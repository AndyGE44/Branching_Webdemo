#!/usr/bin/env bash
# deploy/harden-ports.sh — restrict the demo's internal ports to loopback.
#
# The storefront runtimes (DEMO_BRANCH_PORT_START..END, default 8300-8350) and the
# shop mock-api (:4000) bind 0.0.0.0 and have NO auth of their own — the app is
# meant to be reached only via the control plane on DEMO_MAIN_PORT (Basic Auth,
# through the tunnel). Without a host firewall those ports are reachable on the
# node's public IP, bypassing Basic Auth. This installs an nftables rule (+ a
# boot service) that DROPs all NON-loopback access to those internal ports.
#
# It does NOT touch SSH, the control-plane port, :443, NFS/rpcbind (:111), or
# Tailscale — so it cannot lock you out. Idempotent; safe to re-run.
#
#   sudo ./deploy/harden-ports.sh              # apply + enable on boot
#   sudo ./deploy/harden-ports.sh --uninstall  # remove the rule + service
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SVC=shopgym-demo-firewall.service
NFT_FILE=/etc/shopgym-demo-firewall.nft
UNIT="/etc/systemd/system/$SVC"
TABLE="inet shopgym_demo_fw"

die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
[[ "$(id -u)" -eq 0 ]] || die "run with sudo (installing an nftables rule + unit needs root)."
NFT="$(command -v nft || echo /usr/sbin/nft)"
[[ -x "$NFT" ]] || die "nft (nftables) not found — install it (e.g. apt-get install -y nftables)."

# --- uninstall ---------------------------------------------------------------
if [[ "${1:-}" == "--uninstall" ]]; then
  systemctl disable --now "$SVC" 2>/dev/null || true
  # shellcheck disable=SC2086
  "$NFT" delete table $TABLE 2>/dev/null || true
  rm -f "$UNIT" "$NFT_FILE"
  systemctl daemon-reload
  echo ">> port hardening removed."
  exit 0
fi

# --- port config: .env provides defaults, explicit env vars win --------------
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
MAIN_PORT="${DEMO_MAIN_PORT:-8000}"
BRANCH_START="${DEMO_BRANCH_PORT_START:-8300}"
BRANCH_END="${DEMO_BRANCH_PORT_END:-8350}"
MOCK_PORT="${DEMO_MOCK_API_PORT:-4000}"
DROP_SET="${MOCK_PORT}, ${BRANCH_START}-${BRANCH_END}"

# --- ruleset: allow genuine loopback, drop everything else to those ports -----
# saddr-based (not `iif lo`) so a self-test to the node's own public IP is dropped
# too — the control plane reaches the storefront via 127.0.0.1, which is allowed.
cat > "$NFT_FILE" <<EOF
#!/usr/sbin/nft -f
# Restrict the shopgym demo's internal ports to loopback. The storefront
# ($BRANCH_START-$BRANCH_END) and mock-api ($MOCK_PORT) bind 0.0.0.0 and have NO
# auth of their own; the app is reached only via the control plane on :$MAIN_PORT
# (Basic Auth, through the tunnel). Does not touch SSH, :$MAIN_PORT, :443, NFS
# (:111), or Tailscale. Managed by deploy/harden-ports.sh — edit there. Idempotent.
add table $TABLE
delete table $TABLE
table $TABLE {
    chain input {
        type filter hook input priority 0; policy accept;
        ip  saddr 127.0.0.0/8 accept
        ip6 saddr ::1 accept
        tcp dport { $DROP_SET } drop
    }
}
EOF

"$NFT" -c -f "$NFT_FILE" || die "generated nftables ruleset failed to validate"

# --- boot service (re-applies on boot; idempotent) ---------------------------
cat > "$UNIT" <<EOF
[Unit]
Description=Shopgym demo — restrict internal storefront/mock-api ports to loopback
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$NFT -f $NFT_FILE
ExecReload=$NFT -f $NFT_FILE
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SVC"

echo ">> port hardening active — dropping non-loopback tcp to: $DROP_SET"
# shellcheck disable=SC2086
"$NFT" list table $TABLE
