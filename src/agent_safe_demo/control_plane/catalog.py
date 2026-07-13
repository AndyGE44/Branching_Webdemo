"""The storefront's branchable data: the ``variant_state`` overlay table.

This is the control-plane side of the storefront's Dolt data tier. It owns the
schema and seed of ``variant_state`` (one row per product variant: price,
compare-at price, on-hand stock, availability) and backs the control-plane
"catalog editor" (list / edit price+stock) plus the row-level Dolt diff the UI
shows. The in-runtime mock Storefront API reads the same table as a read-only
*overlay* over its baked JSON catalog (see ``app_plane/mock_api_overlay/``).

Seed source is each shop's ``products.json`` (the same synthetic catalog baked
into the shop image), so the ``variant_id`` keys line up with what the mock API
overlays. ``on_hand`` is absent from the catalog, so it is seeded synthetically.

All access is short-lived PyMySQL connections against the host ``dolt
sql-server`` (the control plane lives outside the checkpoint), keyed to one shop
database.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("control_plane.Catalog")

CREATE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS variant_state (
    variant_id       VARCHAR(64) PRIMARY KEY,
    product_handle   VARCHAR(255) NOT NULL,
    product_title    VARCHAR(255),
    variant_title    VARCHAR(255),
    sku              VARCHAR(255),
    price            DECIMAL(10,2) NOT NULL,
    compare_at_price DECIMAL(10,2),
    on_hand          INT NOT NULL,
    available        TINYINT(1) NOT NULL
)
"""

# Editable columns exposed by the catalog editor.
_EDITABLE = ("price", "compare_at_price", "on_hand")


def products_json_for_app(app_id: str, shopgym_dir: Path) -> Path:
    """Map a shop app id to its host ``products.json``.

    ``shop_clothing`` -> ``<shopgym>/mock_clothing/data/products.json`` etc.
    """
    name = app_id[len("shop_"):] if app_id.startswith("shop_") else app_id
    return Path(shopgym_dir).expanduser() / f"mock_{name}" / "data" / "products.json"


class CatalogStore:
    """Schema + seed + editor operations on ``variant_state`` for one shop db."""

    def __init__(self, conn_params: dict[str, Any]) -> None:
        self.conn_params = dict(conn_params)

    def _connect(self):
        import pymysql

        return pymysql.connect(
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
            **self.conn_params,
        )

    def _query(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return list(cur.fetchall() or [])
        finally:
            conn.close()

    def _exec(self, sql: str, args: tuple = ()) -> int:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return cur.rowcount
        finally:
            conn.close()

    # ---- schema / seed --------------------------------------------------- #
    def ensure_schema(self) -> None:
        self._exec(CREATE_TABLE_DDL)

    def is_empty(self) -> bool:
        return int(next(iter(self._query("SELECT COUNT(*) AS c FROM variant_state")[0].values()))) == 0

    def seed_from_products_json(self, products_path: Path, default_stock: int = 25) -> int:
        """Insert one ``variant_state`` row per variant. Returns rows seeded.

        Idempotent via ``INSERT ... ON DUPLICATE KEY UPDATE`` on the price/stock
        columns, so re-seeding a fresh db is safe.
        """
        products_path = Path(products_path)
        if not products_path.exists():
            logger.warning("catalog seed skipped: %s not found", products_path)
            return 0
        products = json.loads(products_path.read_text())
        if isinstance(products, dict):
            products = products.get("products", [])

        rows: list[tuple] = []
        for product in products:
            handle = product.get("handle") or str(product.get("id"))
            title = product.get("title") or handle
            for variant in product.get("variants", []):
                price = _to_decimal(variant.get("price"))
                if price is None:
                    continue
                rows.append(
                    (
                        str(variant.get("id")),
                        handle,
                        title,
                        variant.get("title"),
                        variant.get("sku"),
                        price,
                        _to_decimal(variant.get("compare_at_price")),
                        default_stock,
                        1 if variant.get("available", True) else 0,
                    )
                )
        if not rows:
            return 0

        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO variant_state "
                    "(variant_id, product_handle, product_title, variant_title, sku, "
                    " price, compare_at_price, on_hand, available) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE price=VALUES(price), "
                    "compare_at_price=VALUES(compare_at_price), on_hand=VALUES(on_hand), "
                    "available=VALUES(available)",
                    rows,
                )
        finally:
            conn.close()
        logger.info("catalog seeded: %d variants from %s", len(rows), products_path)
        return len(rows)

    def truncate(self) -> None:
        self._exec("DELETE FROM variant_state")

    # ---- editor ---------------------------------------------------------- #
    def list_variants(self, limit: int = 500, search: str | None = None) -> list[dict[str, Any]]:
        args: tuple = ()
        where = ""
        if search:
            where = "WHERE product_title LIKE %s OR variant_title LIKE %s OR sku LIKE %s"
            like = f"%{search}%"
            args = (like, like, like)
        rows = self._query(
            "SELECT variant_id, product_handle, product_title, variant_title, sku, "
            "price, compare_at_price, on_hand, available "
            f"FROM variant_state {where} ORDER BY product_title, variant_title "
            "LIMIT %s",
            args + (int(limit),),
        )
        return [_coerce_variant(row) for row in rows]

    def update_variant(self, variant_id: str, **fields: Any) -> dict[str, Any]:
        sets = []
        args: list[Any] = []
        for key, value in fields.items():
            if key not in _EDITABLE or value is None:
                continue
            if key == "on_hand":
                args.append(int(value))
            else:
                args.append(_to_decimal(value))
            sets.append(f"{key} = %s")
        if not sets:
            raise ValueError("No editable fields supplied (price, compare_at_price, on_hand)")
        # Keep availability consistent with stock so the storefront reflects it.
        sets.append("available = (on_hand > 0)")
        args.append(str(variant_id))
        rowcount = self._exec(
            f"UPDATE variant_state SET {', '.join(sets)} WHERE variant_id = %s",
            tuple(args),
        )
        if rowcount == 0:
            # Either the id is unknown or the values were unchanged; distinguish.
            if not self._query("SELECT 1 FROM variant_state WHERE variant_id = %s", (str(variant_id),)):
                raise KeyError(f"Unknown variant: {variant_id}")
        rows = self._query(
            "SELECT variant_id, product_handle, product_title, variant_title, sku, "
            "price, compare_at_price, on_hand, available FROM variant_state WHERE variant_id = %s",
            (str(variant_id),),
        )
        return _coerce_variant(rows[0])

    # ---- diff (working set vs the pristine seed) ------------------------- #
    def diff_vs_clean(self) -> list[dict[str, Any]]:
        """Row-level Dolt diff of ``variant_state`` since the pristine catalog.

        Returns only changed rows, with before/after price + stock. Empty if the
        ``clean`` ref does not exist yet (data tier not marked clean)."""
        try:
            rows = self._query(
                "SELECT to_variant_id, from_variant_id, "
                "to_product_title, from_product_title, "
                "to_variant_title, from_variant_title, "
                "from_price, to_price, from_on_hand, to_on_hand, "
                "from_available, to_available, diff_type "
                "FROM dolt_diff('clean', 'WORKING', 'variant_state')"
            )
        except Exception as error:  # clean ref missing, or dolt_diff unavailable
            logger.debug("catalog diff unavailable: %s", error)
            return []
        changes: list[dict[str, Any]] = []
        for row in rows:
            if row.get("diff_type") == "unchanged":
                continue
            changes.append(
                {
                    "variant_id": row.get("to_variant_id") or row.get("from_variant_id"),
                    "product_title": row.get("to_product_title") or row.get("from_product_title"),
                    "variant_title": row.get("to_variant_title") or row.get("from_variant_title"),
                    "diff_type": row.get("diff_type"),
                    "price": {"from": _num(row.get("from_price")), "to": _num(row.get("to_price"))},
                    "on_hand": {"from": row.get("from_on_hand"), "to": row.get("to_on_hand")},
                    "available": {"from": row.get("from_available"), "to": row.get("to_available")},
                }
            )
        return changes


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _to_decimal(value: Any) -> Any:
    if value is None or value == "":
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_variant(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "variant_id": row.get("variant_id"),
        "product_handle": row.get("product_handle"),
        "product_title": row.get("product_title"),
        "variant_title": row.get("variant_title"),
        "sku": row.get("sku"),
        "price": _num(row.get("price")),
        "compare_at_price": _num(row.get("compare_at_price")),
        "on_hand": int(row["on_hand"]) if row.get("on_hand") is not None else None,
        "available": bool(row.get("available")),
    }


def shopgym_dir_default() -> Path:
    return Path(os.getenv("SHOPGYM_DIR", str(Path.home() / "shopgym")))
