"""Tests for the storefront external-Dolt data tier (architecture A).

Two tiers of tests:

- **No-DB logic** (always runs): env-gating, the runtime-env injection, seed
  helpers, the overlay sync, and that the default path is unchanged.
- **Live Dolt** (skipped unless a ``dolt`` binary is on PATH): a real ephemeral
  ``dolt sql-server`` exercising CatalogStore seed/list/update/diff and
  DoltServerDataTier snapshot/restore versioning.
"""

from __future__ import annotations

import json
import shutil
import socket
import time
from pathlib import Path

import pytest

from agent_safe_demo.control_plane import overlay_sync
from agent_safe_demo.control_plane.catalog import (
    CatalogStore,
    _coerce_variant,
    _to_decimal,
    products_json_for_app,
)
from agent_safe_demo.control_plane.data_tier import DoltServerDataTier
from agent_safe_demo.control_plane.statefork import StateForkBackend
from agent_safe_demo.control_plane.workspace import Workspace

DOLT = shutil.which("dolt")
requires_dolt = pytest.mark.skipif(DOLT is None, reason="dolt binary not on PATH")


# --------------------------------------------------------------------------- #
# No-DB logic (always runs)
# --------------------------------------------------------------------------- #
def test_products_json_for_app_mapping():
    root = Path("/tmp/shopgym")
    assert products_json_for_app("shop_clothing", root) == root / "mock_clothing" / "data" / "products.json"
    assert products_json_for_app("shop_cookware", root) == root / "mock_cookware" / "data" / "products.json"
    # Non-prefixed ids map straight through.
    assert products_json_for_app("inventory", root) == root / "mock_inventory" / "data" / "products.json"


def test_to_decimal():
    assert _to_decimal("61.99") == 61.99
    assert _to_decimal(12) == 12.0
    assert _to_decimal(None) is None
    assert _to_decimal("") is None
    assert _to_decimal("not-a-number") is None


def test_coerce_variant_types():
    row = {
        "variant_id": "10000",
        "product_handle": "hoodie",
        "product_title": "Hoodie",
        "variant_title": "XS",
        "sku": "SKU-1",
        "price": "61.99",
        "compare_at_price": None,
        "on_hand": 25,
        "available": 1,
    }
    out = _coerce_variant(row)
    assert out["price"] == 61.99
    assert out["compare_at_price"] is None
    assert out["on_hand"] == 25
    assert out["available"] is True


def test_dolt_branch_name():
    tier = DoltServerDataTier(database="shop_clothing")
    assert tier.branch_name("abc123") == "sf_abc123"


def test_set_data_tier_injects_runtime_env():
    """set_data_tier attaches the tier and injects the mock-api's DB env."""
    spec = Workspace().app  # any resolved AppSpec
    backend = StateForkBackend(spec, statefork_root=Path("/nonexistent"))
    assert backend.data_tier is None
    sentinel = object()
    backend.set_data_tier(sentinel, runtime_env={"SHOP_DOLT_HOST": "127.0.0.1", "SHOP_DOLT_DB": "shop_clothing"})
    assert backend.data_tier is sentinel
    assert backend.runtime_manager.env["SHOP_DOLT_HOST"] == "127.0.0.1"
    assert backend.runtime_manager.env["SHOP_DOLT_DB"] == "shop_clothing"


def test_data_tier_hooks_are_noops_without_tier():
    """The snapshot/restore/cleanup hooks must be inert when no tier is set, so
    the default in-checkpoint behaviour is unchanged."""
    spec = Workspace().app
    backend = StateForkBackend(spec, statefork_root=Path("/nonexistent"))
    # Should neither raise nor do anything.
    backend._data_tier_snapshot("snap-1")
    backend._data_tier_restore("snap-1")
    backend._data_tier_cleanup()


def test_workspace_data_tier_disabled_by_default():
    """Without a dolt server attached, the workspace exposes no catalog and the
    payload reports the data tier as disabled."""
    ws = Workspace()
    assert ws.catalog is None
    assert ws.data_tier is None
    payload = ws.payload(
        {"id": "sf-x", "url": "http://127.0.0.1:8300", "base_id": None, "current_snapshot_id": None}
    )
    assert payload["data_tier"]["enabled"] is False
    with pytest.raises(Exception):
        ws.catalog_list()


def test_overlay_sync_populates_shop_dirs():
    """The overlay sync copies the canonical *.ts into each shop build dir."""
    updated = overlay_sync.sync_shop_overlay_sources()
    assert updated, "expected at least one shop dir to be synced"
    for shop_dir in updated:
        overlay = shop_dir / overlay_sync.SHOP_OVERLAY_SUBDIR
        assert (overlay / "overlay.ts").exists()
        assert (overlay / "data.ts").exists()
        assert (overlay / "server.ts").exists()


# --------------------------------------------------------------------------- #
# Live Dolt (skipped unless `dolt` is installed)
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def dolt_server(tmp_path):
    """A live ephemeral dolt sql-server hosting one database, torn down after."""
    from agent_safe_demo.control_plane.dolt_server import DoltSqlServer

    server = DoltSqlServer(tmp_path / "dolt", host="127.0.0.1", port=_free_port())
    server.start()
    server.ensure_database("shop_test")
    try:
        yield server
    finally:
        server.stop()


_SEED_PRODUCTS = [
    {
        "handle": "hoodie",
        "title": "Plush Hoodie",
        "variants": [
            {"id": 10000, "sku": "H-XS", "price": "61.99", "compare_at_price": "127.99", "available": True},
            {"id": 10001, "sku": "H-S", "price": "61.99", "compare_at_price": None, "available": True},
        ],
    }
]


@requires_dolt
def test_catalog_seed_list_update_diff(dolt_server, tmp_path):
    products = tmp_path / "products.json"
    products.write_text(json.dumps(_SEED_PRODUCTS))
    conn = dolt_server.conn_params("shop_test")
    catalog = CatalogStore(conn)
    catalog.ensure_schema()
    assert catalog.is_empty()
    n = catalog.seed_from_products_json(products, default_stock=25)
    assert n == 2
    assert not catalog.is_empty()

    variants = catalog.list_variants()
    assert {v["variant_id"] for v in variants} == {"10000", "10001"}
    assert all(v["on_hand"] == 25 for v in variants)

    # Pin a clean baseline, then edit and confirm the diff shows only the change.
    tier = DoltServerDataTier(host=conn["host"], port=conn["port"], database="shop_test")
    tier.mark_clean()
    assert catalog.diff_vs_clean() == []

    catalog.update_variant("10000", price=49.99, on_hand=3)
    changes = catalog.diff_vs_clean()
    changed_ids = {c["variant_id"] for c in changes}
    assert "10000" in changed_ids and "10001" not in changed_ids
    updated = next(v for v in catalog.list_variants() if v["variant_id"] == "10000")
    assert updated["price"] == 49.99 and updated["on_hand"] == 3


@requires_dolt
def test_data_tier_snapshot_restore_roundtrip(dolt_server, tmp_path):
    products = tmp_path / "products.json"
    products.write_text(json.dumps(_SEED_PRODUCTS))
    conn = dolt_server.conn_params("shop_test")
    catalog = CatalogStore(conn)
    catalog.ensure_schema()
    catalog.seed_from_products_json(products, default_stock=25)

    tier = DoltServerDataTier(host=conn["host"], port=conn["port"], database="shop_test")
    tier.mark_clean()

    # Snapshot v1 at the seeded price.
    tier.on_snapshot("v1")
    # Edit the working set (price change), then snapshot v2.
    catalog.update_variant("10000", price=10.00)
    tier.on_snapshot("v2")
    assert next(v for v in catalog.list_variants() if v["variant_id"] == "10000")["price"] == 10.00

    # Restore v1 rolls the price back; restore v2 rolls it forward.
    tier.on_restore("v1")
    assert next(v for v in catalog.list_variants() if v["variant_id"] == "10000")["price"] == 61.99
    tier.on_restore("v2")
    assert next(v for v in catalog.list_variants() if v["variant_id"] == "10000")["price"] == 10.00

    # summary/fingerprint reflect the current state and change when data changes.
    fp_before = tier.fingerprint()
    catalog.update_variant("10001", on_hand=0)
    assert tier.fingerprint() != fp_before
    summary = tier.summary()
    assert "variant_state" in summary["counts"]
    assert summary["counts"]["variant_state"] == 2

    tier.cleanup()  # prunes sf_* branches; should not raise


@requires_dolt
def test_merge_into_working(dolt_server, tmp_path):
    import pymysql

    products = tmp_path / "products.json"
    products.write_text(json.dumps(_SEED_PRODUCTS))
    conn = dolt_server.conn_params("shop_test")
    catalog = CatalogStore(conn)
    catalog.ensure_schema()
    catalog.seed_from_products_json(products, default_stock=25)
    tier = DoltServerDataTier(host=conn["host"], port=conn["port"], database="shop_test")
    tier.mark_clean()

    # Two branches diverging from clean: sf_a edits price, sf_b edits stock.
    c = pymysql.connect(autocommit=True, cursorclass=pymysql.cursors.DictCursor, **conn)
    cur = c.cursor()

    def run(sql):
        cur.execute(sql)

    run("CALL DOLT_BRANCH('sf_base','clean')")
    run("CALL DOLT_BRANCH('sf_a','clean')")
    run("CALL DOLT_CHECKOUT('sf_a')")
    run("UPDATE variant_state SET price=5.00 WHERE variant_id='10000'")
    run("CALL DOLT_COMMIT('-a','-m','a')")
    run("CALL DOLT_CHECKOUT('main')")
    run("CALL DOLT_BRANCH('sf_b','clean')")
    run("CALL DOLT_CHECKOUT('sf_b')")
    run("UPDATE variant_state SET on_hand=999 WHERE variant_id='10001'")
    run("CALL DOLT_COMMIT('-a','-m','b')")
    run("CALL DOLT_CHECKOUT('main')")

    # Clean cell-level merge: both land.
    assert tier.merge_into_working("base", ["a", "b"]) == []
    items = {v["variant_id"]: v for v in catalog.list_variants()}
    assert items["10000"]["price"] == 5.00 and items["10001"]["on_hand"] == 999

    # Now make sf_b also edit 10000's price -> conflict on the same cell.
    run("CALL DOLT_CHECKOUT('sf_b')")
    run("UPDATE variant_state SET price=7.77 WHERE variant_id='10000'")
    run("CALL DOLT_COMMIT('-a','-m','b2')")
    run("CALL DOLT_CHECKOUT('main')")
    conflicts = tier.merge_into_working("base", ["a", "b"])
    assert "10000" in conflicts
    # aborted + rolled back to base (clean) -> 10000 is the seed price again
    assert {v["variant_id"]: v for v in catalog.list_variants()}["10000"]["price"] == 61.99
    c.close()
