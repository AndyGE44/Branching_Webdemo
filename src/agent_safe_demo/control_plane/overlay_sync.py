"""Keep each shop build dir stocked with the mock-api overlay source.

The external-Dolt overlay (``app_plane/mock_api_overlay/``) is the canonical,
reviewed source. Each shop's Dockerfile ``COPY mock-api-overlay/`` needs those
files inside its own build context (Waypoint/buildah builds with the shop dir as
context), so this module copies the canonical files into a generated
``mock-api-overlay/`` dir under each ``shop_*`` app before any image is built.

The generated per-shop copies are gitignored; run this at control-plane startup
(idempotent) or via ``scripts/sync-mock-api-overlay.sh``.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger("control_plane.overlay_sync")

APP_PLANE_DIR = Path(__file__).resolve().parents[1] / "app_plane"
CANONICAL_DIR = APP_PLANE_DIR / "mock_api_overlay"
# Name of the generated dir inside each shop build context (matches the
# `COPY mock-api-overlay/ ...` in each shop Dockerfile).
SHOP_OVERLAY_SUBDIR = "mock-api-overlay"


def sync_shop_overlay_sources() -> list[Path]:
    """Copy the canonical overlay ``*.ts`` into each ``shop_*/mock-api-overlay/``.

    Idempotent and best-effort. Returns the list of shop dirs updated.
    """
    if not CANONICAL_DIR.is_dir():
        logger.warning("overlay canonical dir missing: %s", CANONICAL_DIR)
        return []
    sources = sorted(CANONICAL_DIR.glob("*.ts"))
    if not sources:
        logger.warning("no overlay sources found in %s", CANONICAL_DIR)
        return []

    updated: list[Path] = []
    for shop_dir in sorted(APP_PLANE_DIR.glob("shop_*")):
        if not (shop_dir / "Dockerfile").exists():
            continue
        dest = shop_dir / SHOP_OVERLAY_SUBDIR
        dest.mkdir(exist_ok=True)
        for src in sources:
            shutil.copy2(src, dest / src.name)
        updated.append(shop_dir)
    logger.info(
        "synced %d overlay files into %d shop build dirs",
        len(sources),
        len(updated),
    )
    return updated
