"""Pluggable data tier for the inventory service.

Two interchangeable backends, selected at runtime:

- ``SqliteInventoryStore`` — the original file-backed SQLite tier. The DB file
  lives inside the StateFork-managed workspace, so StateFork captures it as part
  of the checkpoint (architecture **B**: data inside the snapshot).
- ``DoltInventoryStore`` — an *external* Dolt database the app talks to via the
  ``dolt`` CLI. The Dolt repo lives outside the checkpoint and is versioned by
  StateFork's ``DoltController`` using Dolt's own branching (architecture **A**:
  data tier branched independently of the app snapshot).

This module is intentionally dependency-free (stdlib only) so it can be unit
tested without FastAPI. The web layer in ``app.py`` maps the domain exceptions
below onto HTTP responses.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# Seed data shared by both backends so they start from an identical state.
SEED_PARTS: tuple[tuple[str, str, str, int, int], ...] = (
    ("MCU-100", "Control board", "Aisle 1 / Bin 02", 8, 4),
    ("MCU-ALT", "Backup control board", "Aisle 1 / Bin 08", 4, 2),
    ("SENSOR-9", "Temperature sensor", "Aisle 3 / Bin 11", 2, 5),
    ("CASE-42", "Aluminum enclosure", "Aisle 4 / Bin 01", 12, 4),
    ("WIRE-RED", "Red harness wire", "Aisle 2 / Bin 05", 50, 10),
)


class InventoryError(Exception):
    """Base class for inventory domain errors."""


class UnknownPart(InventoryError):
    def __init__(self, part_id: str) -> None:
        super().__init__(f"Unknown part: {part_id}")
        self.part_id = part_id


class InsufficientStock(InventoryError):
    """Raised when a sell/reserve exceeds available quantity."""


class InventoryStore(ABC):
    """Backend-agnostic interface used by the inventory web app."""

    backend: str = "base"

    @abstractmethod
    def init(self) -> None: ...

    @abstractmethod
    def inventory_items(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def inventory_item(self, part_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def buy(self, part_id: str, quantity: int, actor: str) -> dict[str, Any]: ...

    @abstractmethod
    def sell(self, part_id: str, quantity: int, actor: str) -> dict[str, Any]: ...

    @abstractmethod
    def reserve(self, part_id: str, quantity: int, actor: str) -> dict[str, Any]: ...

    @abstractmethod
    def state(self) -> dict[str, Any]: ...

    @abstractmethod
    def reset(self) -> None: ...

    @property
    @abstractmethod
    def location_label(self) -> str:
        """Human-readable location of the data tier (shown in /api/state)."""


# --------------------------------------------------------------------------- #
# SQLite backend (architecture B: data captured inside the StateFork snapshot)
# --------------------------------------------------------------------------- #
class SqliteInventoryStore(InventoryStore):
    backend = "sqlite"

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    @property
    def location_label(self) -> str:
        return str(self.db_path)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _audit(conn: sqlite3.Connection, actor: str, action: str, detail: str) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor, action, detail) VALUES (?, ?, ?)",
            (actor, action, detail),
        )

    @staticmethod
    def _ensure_part(conn: sqlite3.Connection, part_id: str) -> sqlite3.Row:
        part = conn.execute("SELECT * FROM parts WHERE id = ?", (part_id,)).fetchone()
        if part is None:
            raise UnknownPart(part_id)
        return part

    _ITEM_SELECT = """
        SELECT
            p.id, p.name, p.location, p.on_hand, p.reorder_point,
            p.on_hand - COALESCE(SUM(r.quantity), 0) AS available,
            COALESCE(SUM(r.quantity), 0) AS reserved
        FROM parts p
        LEFT JOIN reservations r ON r.part_id = p.id AND r.status = 'active'
    """

    def _item(self, conn: sqlite3.Connection, part_id: str) -> dict[str, Any]:
        row = conn.execute(
            self._ITEM_SELECT + " WHERE p.id = ? GROUP BY p.id", (part_id,)
        ).fetchone()
        if row is None:
            raise UnknownPart(part_id)
        return dict(row)

    def _items(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in conn.execute(self._ITEM_SELECT + " GROUP BY p.id ORDER BY p.id")
        ]

    def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS parts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    location TEXT NOT NULL,
                    on_hand INTEGER NOT NULL CHECK(on_hand >= 0),
                    reorder_point INTEGER NOT NULL CHECK(reorder_point >= 0)
                );

                CREATE TABLE IF NOT EXISTS reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    part_id TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(part_id) REFERENCES parts(id)
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            if not conn.execute("SELECT COUNT(*) AS c FROM parts").fetchone()["c"]:
                conn.executemany(
                    "INSERT INTO parts(id, name, location, on_hand, reorder_point) "
                    "VALUES (?, ?, ?, ?, ?)",
                    SEED_PARTS,
                )
            if not conn.execute("SELECT COUNT(*) AS c FROM audit_log").fetchone()["c"]:
                self._audit(conn, "system", "seed", "Loaded sample inventory data")

    def inventory_items(self) -> list[dict[str, Any]]:
        with self._db() as conn:
            return self._items(conn)

    def inventory_item(self, part_id: str) -> dict[str, Any]:
        with self._db() as conn:
            return self._item(conn, part_id)

    def buy(self, part_id: str, quantity: int, actor: str) -> dict[str, Any]:
        with self._db() as conn:
            self._ensure_part(conn, part_id)
            conn.execute(
                "UPDATE parts SET on_hand = on_hand + ? WHERE id = ?", (quantity, part_id)
            )
            self._audit(conn, actor, "buy", f"Bought {quantity} units of {part_id}")
            return self._item(conn, part_id)

    def sell(self, part_id: str, quantity: int, actor: str) -> dict[str, Any]:
        with self._db() as conn:
            self._ensure_part(conn, part_id)
            available = self._item(conn, part_id)["available"]
            if quantity > available:
                raise InsufficientStock(
                    f"Only {available} units of {part_id} are available to sell"
                )
            conn.execute(
                "UPDATE parts SET on_hand = on_hand - ? WHERE id = ?", (quantity, part_id)
            )
            self._audit(conn, actor, "sell", f"Sold {quantity} units of {part_id}")
            return self._item(conn, part_id)

    def reserve(self, part_id: str, quantity: int, actor: str) -> dict[str, Any]:
        with self._db() as conn:
            self._ensure_part(conn, part_id)
            available = self._item(conn, part_id)["available"]
            if quantity > available:
                raise InsufficientStock(
                    f"Only {available} units of {part_id} are available"
                )
            cursor = conn.execute(
                "INSERT INTO reservations(part_id, quantity, status, actor) "
                "VALUES (?, ?, 'active', ?)",
                (part_id, quantity, actor),
            )
            self._audit(conn, actor, "reserve", f"Reserved {quantity} units of {part_id}")
            return {"reservation_id": cursor.lastrowid, "status": "active"}

    def state(self) -> dict[str, Any]:
        with self._db() as conn:
            items = self._items(conn)
            reservations = [
                dict(row)
                for row in conn.execute("SELECT * FROM reservations ORDER BY id DESC")
            ]
            audit_log = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM audit_log ORDER BY id DESC LIMIT 25"
                )
            ]
        return _state_payload(items, reservations, audit_log)

    def reset(self) -> None:
        if self.db_path.exists():
            self.db_path.unlink()
        self.init()


# --------------------------------------------------------------------------- #
# Dolt backend (architecture A: external data tier branched by StateFork)
# --------------------------------------------------------------------------- #
class DoltInventoryStore(InventoryStore):
    """Talks to an external Dolt repo via the ``dolt`` CLI working set.

    Writes land in the repo's working set (not a Dolt commit). StateFork's
    ``DoltController`` is what turns each StateFork snapshot into a Dolt commit
    + branch and resets the working set on restore, so the data tier follows the
    app snapshot without the DB ever being copied into the checkpoint.
    """

    backend = "dolt"

    def __init__(self, repo_dir: Path, dolt_bin: str = "dolt") -> None:
        self.repo_dir = Path(repo_dir)
        self.dolt_bin = dolt_bin

    @property
    def location_label(self) -> str:
        return f"{self.repo_dir} (dolt)"

    # ---- low-level CLI helpers ------------------------------------------- #
    @staticmethod
    def _lit(value: Any) -> str:
        """Quote a value as a SQL string literal (CLI has no bind params).

        Note: this is a correctness-first path for the demo. The benchmark path
        will move to ``dolt sql-server`` + a driver with real bind parameters.
        """
        text = str(value).replace("\\", "\\\\").replace("'", "''")
        return f"'{text}'"

    def _sql(self, query: str, want_rows: bool = False) -> list[dict[str, Any]]:
        if shutil.which(self.dolt_bin) is None:
            raise InventoryError(
                f"`{self.dolt_bin}` not found on PATH; cannot use the Dolt backend."
            )
        args = [self.dolt_bin, "sql", "-q", query]
        if want_rows:
            args += ["-r", "json"]
        proc = subprocess.run(args, cwd=self.repo_dir, capture_output=True, text=True)
        if proc.returncode != 0:
            raise InventoryError(
                f"dolt sql failed (rc={proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}\nQuery: {query}"
            )
        if not want_rows:
            return []
        out = proc.stdout.strip()
        if not out:
            return []
        return json.loads(out).get("rows", [])

    def _scalar(self, query: str) -> Any:
        rows = self._sql(query, want_rows=True)
        if not rows:
            return None
        return next(iter(rows[0].values()))

    # ---- schema / seed --------------------------------------------------- #
    def init(self) -> None:
        if shutil.which(self.dolt_bin) is None:
            raise InventoryError(
                f"`{self.dolt_bin}` not found on PATH; cannot use the Dolt backend."
            )
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        if not (self.repo_dir / ".dolt").is_dir():
            subprocess.run(
                [self.dolt_bin, "init", "--name", "InventoryApp",
                 "--email", "inventory@local", "--initial-branch", "main"],
                cwd=self.repo_dir, capture_output=True, text=True, check=True,
            )

        self._sql(
            """
            CREATE TABLE IF NOT EXISTS parts (
                id VARCHAR(64) PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                location VARCHAR(255) NOT NULL,
                on_hand INT NOT NULL CHECK (on_hand >= 0),
                reorder_point INT NOT NULL CHECK (reorder_point >= 0)
            )
            """
        )
        self._sql(
            """
            CREATE TABLE IF NOT EXISTS reservations (
                id INT PRIMARY KEY AUTO_INCREMENT,
                part_id VARCHAR(64) NOT NULL,
                quantity INT NOT NULL CHECK (quantity > 0),
                status VARCHAR(32) NOT NULL,
                actor VARCHAR(255) NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (part_id) REFERENCES parts(id)
            )
            """
        )
        self._sql(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INT PRIMARY KEY AUTO_INCREMENT,
                actor VARCHAR(255) NOT NULL,
                action VARCHAR(255) NOT NULL,
                detail TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._seed_if_empty()

    def _seed_if_empty(self) -> None:
        if int(self._scalar("SELECT COUNT(*) FROM parts") or 0) == 0:
            values = ", ".join(
                f"({self._lit(pid)}, {self._lit(name)}, {self._lit(loc)}, {oh}, {rp})"
                for pid, name, loc, oh, rp in SEED_PARTS
            )
            self._sql(
                "INSERT INTO parts(id, name, location, on_hand, reorder_point) "
                f"VALUES {values}"
            )
        if int(self._scalar("SELECT COUNT(*) FROM audit_log") or 0) == 0:
            self._audit("system", "seed", "Loaded sample inventory data")

    def _audit(self, actor: str, action: str, detail: str) -> None:
        self._sql(
            "INSERT INTO audit_log(actor, action, detail) VALUES "
            f"({self._lit(actor)}, {self._lit(action)}, {self._lit(detail)})"
        )

    # ---- queries --------------------------------------------------------- #
    _ITEM_SELECT = """
        SELECT
            p.id, p.name, p.location, p.on_hand, p.reorder_point,
            p.on_hand - COALESCE(SUM(r.quantity), 0) AS available,
            COALESCE(SUM(r.quantity), 0) AS reserved
        FROM parts p
        LEFT JOIN reservations r ON r.part_id = p.id AND r.status = 'active'
    """
    _ITEM_GROUP = " GROUP BY p.id, p.name, p.location, p.on_hand, p.reorder_point"

    @staticmethod
    def _coerce_item(row: dict[str, Any]) -> dict[str, Any]:
        for key in ("on_hand", "reorder_point", "available", "reserved"):
            if key in row and row[key] is not None:
                row[key] = int(row[key])
        return row

    def inventory_items(self) -> list[dict[str, Any]]:
        rows = self._sql(self._ITEM_SELECT + self._ITEM_GROUP + " ORDER BY p.id", want_rows=True)
        return [self._coerce_item(row) for row in rows]

    def inventory_item(self, part_id: str) -> dict[str, Any]:
        rows = self._sql(
            self._ITEM_SELECT + f" WHERE p.id = {self._lit(part_id)}" + self._ITEM_GROUP,
            want_rows=True,
        )
        if not rows:
            raise UnknownPart(part_id)
        return self._coerce_item(rows[0])

    def _ensure_part(self, part_id: str) -> None:
        rows = self._sql(
            f"SELECT id FROM parts WHERE id = {self._lit(part_id)}", want_rows=True
        )
        if not rows:
            raise UnknownPart(part_id)

    def buy(self, part_id: str, quantity: int, actor: str) -> dict[str, Any]:
        self._ensure_part(part_id)
        self._sql(
            f"UPDATE parts SET on_hand = on_hand + {int(quantity)} "
            f"WHERE id = {self._lit(part_id)}"
        )
        self._audit(actor, "buy", f"Bought {quantity} units of {part_id}")
        return self.inventory_item(part_id)

    def sell(self, part_id: str, quantity: int, actor: str) -> dict[str, Any]:
        item = self.inventory_item(part_id)
        if quantity > item["available"]:
            raise InsufficientStock(
                f"Only {item['available']} units of {part_id} are available to sell"
            )
        self._sql(
            f"UPDATE parts SET on_hand = on_hand - {int(quantity)} "
            f"WHERE id = {self._lit(part_id)}"
        )
        self._audit(actor, "sell", f"Sold {quantity} units of {part_id}")
        return self.inventory_item(part_id)

    def reserve(self, part_id: str, quantity: int, actor: str) -> dict[str, Any]:
        item = self.inventory_item(part_id)
        if quantity > item["available"]:
            raise InsufficientStock(
                f"Only {item['available']} units of {part_id} are available"
            )
        self._sql(
            "INSERT INTO reservations(part_id, quantity, status, actor) VALUES "
            f"({self._lit(part_id)}, {int(quantity)}, 'active', {self._lit(actor)})"
        )
        reservation_id = int(self._scalar("SELECT MAX(id) FROM reservations") or 0)
        self._audit(actor, "reserve", f"Reserved {quantity} units of {part_id}")
        return {"reservation_id": reservation_id, "status": "active"}

    def state(self) -> dict[str, Any]:
        items = self.inventory_items()
        reservations = self._sql(
            "SELECT id, part_id, quantity, status, actor, created_at "
            "FROM reservations ORDER BY id DESC",
            want_rows=True,
        )
        audit_log = self._sql(
            "SELECT id, actor, action, detail, created_at "
            "FROM audit_log ORDER BY id DESC LIMIT 25",
            want_rows=True,
        )
        return _state_payload(items, reservations, audit_log)

    def reset(self) -> None:
        # Working-set reset to seed state (FK-safe order). StateFork handles the
        # versioned rollback; this is the app-level "/api/reset".
        self._sql("DELETE FROM reservations")
        self._sql("DELETE FROM audit_log")
        self._sql("DELETE FROM parts")
        self._seed_if_empty()


def _state_payload(
    items: list[dict[str, Any]],
    reservations: list[dict[str, Any]],
    audit_log: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "summary": {
            "items": len(items),
            "reservations": len(reservations),
            "low_stock": sum(1 for item in items if item["available"] < item["reorder_point"]),
        },
        "inventory": items,
        "reservations": reservations,
        "audit_log": audit_log,
    }


def create_store(
    *,
    sqlite_path: Path,
    backend: str | None = None,
    dolt_dir: Path | None = None,
    dolt_bin: str | None = None,
) -> InventoryStore:
    """Build the configured inventory store.

    Selection order: explicit ``backend`` arg, else ``DEMO_INVENTORY_DB_BACKEND``
    (``sqlite`` default). For the Dolt backend the repo dir comes from
    ``dolt_dir`` / ``DEMO_INVENTORY_DOLT_DIR`` (default: alongside the SQLite path).
    """
    backend = (backend or os.getenv("DEMO_INVENTORY_DB_BACKEND", "sqlite")).lower()
    if backend == "dolt":
        repo = (
            dolt_dir
            or os.getenv("DEMO_INVENTORY_DOLT_DIR")
            or Path(sqlite_path).with_name("demo_inventory_dolt")
        )
        return DoltInventoryStore(
            Path(repo), dolt_bin=dolt_bin or os.getenv("DEMO_DOLT_BIN", "dolt")
        )
    if backend != "sqlite":
        raise InventoryError(f"Unknown inventory backend: {backend!r}")
    return SqliteInventoryStore(Path(sqlite_path))
