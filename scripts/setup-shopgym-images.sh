#!/usr/bin/env bash
# setup-shopgym-images.sh — bake each shop's product images into its base
# container image.
#
# The shopgym product images ship only as runtime bind-mounts in the standalone
# shopgym setup (~/shopgym/mock_*/.../images), NOT inside the container images.
# StateFork/Waypoint builds the Dockerfile and checkpoints the process with no
# bind mounts, so the Hydrogen storefront (which serves /images from
# /app/data/images) would 404 every product image. This one-time, idempotent step
# copies the images into each base image's /app/data/images so the embedded
# storefront shows its pictures.
#
# Run it once after ~/shopgym/restore.sh (which produces the base images) and
# before/after the control plane is started; then rebuild the workspace.
set -euo pipefail

SHOPGYM_DIR="${SHOPGYM_DIR:-$HOME/shopgym}"
# Stage extraction on the big data disk if present (images are several GB).
TMP_ROOT="${SHOPGYM_IMG_TMP:-$([ -d /mydata ] && echo /mydata/shopgym-img-stage || echo /tmp/shopgym-img-stage)}"

# shop | base image | images path *inside the zip* (cookware differs!)
SHOPS=(
  "clothing|localhost/shop-arena-mock-clothing:latest|mock_clothing/data/images"
  "cookware|localhost/shop-arena-mock-cookware:latest|mock_cookware/images"
  "hardware|localhost/shop-arena-mock-hardware:latest|mock_hardware/data/images"
)

command -v sudo >/dev/null 2>&1 || { echo "sudo is required (root container storage)." >&2; exit 1; }
command -v buildah >/dev/null 2>&1 || { echo "buildah is required." >&2; exit 1; }

# Make the staging root writable by this user (the data disk /mydata is root-owned).
sudo mkdir -p "$TMP_ROOT"
sudo chown "$(id -u):$(id -g)" "$TMP_ROOT"

for entry in "${SHOPS[@]}"; do
  IFS='|' read -r shop image imgpath <<<"$entry"
  zip="$SHOPGYM_DIR/mock_${shop}.zip"

  if ! sudo podman image exists "$image" 2>/dev/null; then
    echo ">> $shop: base image $image not loaded yet, skipping (run ~/shopgym/restore.sh)" >&2
    continue
  fi

  # Idempotent: skip if the base image already has product images baked in.
  have=$(sudo podman run --rm --entrypoint sh "$image" -c \
    'ls /app/data/images 2>/dev/null | grep -ciE "\.(png|jpg|jpeg|webp)$" || true' 2>/dev/null)
  have=$(printf '%s' "$have" | tr -dc '0-9'); have=${have:-0}
  if [ "$have" -gt 0 ]; then
    echo ">> $shop: $have images already baked, skipping"
    continue
  fi
  [ -f "$zip" ] || { echo ">> $shop: $zip missing, skipping" >&2; continue; }

  stage="$TMP_ROOT/$shop"
  echo ">> $shop: extracting images from $(basename "$zip") (this takes a minute)"
  sudo rm -rf "$stage"; mkdir -p "$stage"
  unzip -q -o "$zip" "$imgpath/*" -x "__MACOSX/*" -d "$stage"

  src="$stage/$imgpath"
  n=$(find "$src" -type f 2>/dev/null | wc -l)
  [ "$n" -gt 0 ] || { echo ">> $shop: no images found under $imgpath, skipping" >&2; rm -rf "$stage"; continue; }

  echo ">> $shop: baking $n images into $image (/app/data/images)"
  cid=$(sudo buildah from "$image")
  sudo buildah copy "$cid" "$src/." /app/data/images >/dev/null
  sudo buildah commit --rm "$cid" "$image" >/dev/null
  rm -rf "$stage"
  echo ">> $shop: done"
done
echo "All available shop images baked. Rebuild the workspace (Reset in the UI) to pick them up."
