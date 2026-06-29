#!/usr/bin/env bash
# deploy/deploy.sh — one-command bring-up of the shopgym StateFork demo on a fresh
# CloudLab node (same project, so the shopgym archive is fetched from the project
# NFS). It provisions the host, clones+pins the sibling repos, restores the shop
# images, builds the artifacts, and launches the control plane.
#
#   Prereqs on the fresh node:
#     - You already cloned THIS repo and are running this script from it.
#     - An ssh-agent with access to the private GitHub repos is forwarded
#       (the script clones Andy_StateFork / Andy_Waypoint over SSH).
#     - sudo is available (CRIU/podman need root).
#
#   Usage:
#     ./deploy/deploy.sh                 # provision + build + launch
#     ./deploy/deploy.sh --no-launch     # provision + build, then stop
#     SHOPGYM_SRC=/path ./deploy/deploy.sh   # override the archive location
#
# Pinned versions live in deploy/versions.env.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
# shellcheck disable=SC1091
source "$HERE/versions.env"

LAUNCH=1
[[ "${1:-}" == "--no-launch" ]] && LAUNCH=0

say() { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

command -v sudo >/dev/null 2>&1 || die "sudo is required (CRIU/podman need root)."
command -v git  >/dev/null 2>&1 || die "git is required."

# ---------------------------------------------------------------------------
say "1/5  Install system packages (podman, buildah, criu, go, python venv)"
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    podman buildah criu golang-go python3-venv python3-pip git unzip lsof rsync \
    || die "apt-get install failed"
else
  echo "    Non-apt host: ensure podman, buildah, criu, go, python3-venv are installed." >&2
fi
# Prefer a manually-installed Go (/usr/local/go) over an old apt golang-go.
[[ -x /usr/local/go/bin/go ]] && export PATH="/usr/local/go/bin:$PATH"
command -v go >/dev/null 2>&1 || die "Go toolchain not found (need it to build Waypoint)."
go_ver="$(go env GOVERSION 2>/dev/null | sed 's/^go//')"
case "$go_ver" in
  1.1[0-9]|1.1[0-9].*) die "Go $go_ver is too old; Waypoint needs >= 1.20. Install a newer Go in /usr/local/go." ;;
esac
echo "    using go $go_ver"

# ---------------------------------------------------------------------------
say "2/5  Clone + pin sibling repos"
clone_pin() { # name url ref
  local dir="$DEPLOY_WORKDIR/$1"
  if [[ ! -d "$dir/.git" ]]; then
    echo "    cloning $1"
    git clone -q "$2" "$dir" || die "clone $1 failed (is the ssh-agent forwarded?)"
  fi
  git -C "$dir" fetch -q --all
  git -C "$dir" checkout -q "$3" || die "checkout $3 in $1 failed"
  echo "    $1 @ $(git -C "$dir" rev-parse --short HEAD)"
}
clone_pin Andy_StateFork "$STATEFORK_URL" "$STATEFORK_REF"
clone_pin Andy_Waypoint  "$WAYPOINT_URL"  "$WAYPOINT_REF"

local_ref="$(git -C "$REPO_ROOT" rev-parse HEAD)"
if [[ "$local_ref" != "$WEBDEMO_REF" ]]; then
  echo "    WARN: this repo is at ${local_ref:0:12}, pinned is ${WEBDEMO_REF:0:12}." >&2
  echo "          checkout $WEBDEMO_REF if you want the exact verified build." >&2
fi

# ---------------------------------------------------------------------------
say "3/5  Restore shopgym data + shop images"
if [[ ! -d "$HOME/shopgym" ]]; then
  [[ -d "$SHOPGYM_SRC" ]] || die "shopgym archive not found at $SHOPGYM_SRC (set SHOPGYM_SRC=...)."
  mkdir -p "$HOME/shopgym"
  echo "    copying archive from $SHOPGYM_SRC (3 GB, takes a minute)"
  cp -an "$SHOPGYM_SRC/." "$HOME/shopgym/"
  chmod +x "$HOME/shopgym"/*.sh 2>/dev/null || true
fi
"$HOME/shopgym/restore.sh"   # unzip mock data + shop image tarballs

# ---------------------------------------------------------------------------
say "4/5  Build artifacts (venv, waypoint, baked product images)"
cd "$REPO_ROOT"
if [[ ! -x .venv/bin/uvicorn ]]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -e '.[dev]'

( cd "$DEPLOY_WORKDIR/Andy_Waypoint" \
    && go build -o waypoint ./cmd/waypoint \
    && go build -o bash_init ./cmd/bash-init )
echo "    waypoint + bash_init built"

./scripts/setup-shopgym-images.sh   # bake product images into the base images

# ---------------------------------------------------------------------------
if [[ "$LAUNCH" -eq 0 ]]; then
  say "Done (provision + build). Launch with: ./scripts/run-shopgym-statefork.sh"
  exit 0
fi
say "5/5  Launch control plane (http://0.0.0.0:8000)"
exec ./scripts/run-shopgym-statefork.sh
