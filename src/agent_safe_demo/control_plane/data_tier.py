"""Control-plane data tier: version an external Dolt database in lockstep with
StateFork snapshots (architecture A).

``StateForkBackend`` historically captured all runtime state *inside* the CRIU
checkpoint (architecture B: the in-memory cart). This tier adds an *external*
data tier — the storefront's pricing/inventory in a Dolt database on the host —
that is versioned by Dolt's own branches, one per StateFork snapshot id
(``sf_<id>``), so restoring a checkpoint also rolls the catalog data back.

The tier talks to a long-lived ``dolt sql-server`` over the MySQL protocol
(PyMySQL), so there is no per-call ``dolt`` process spawn and no CLI contention
with the running server. Versioning uses Dolt's SQL procedures:

- ``on_snapshot(id)``  → ``DOLT_ADD('-A')`` + ``DOLT_COMMIT`` + ``DOLT_BRANCH sf_<id>``
- ``on_restore(id)``   → ``DOLT_CHECKOUT(working)`` + ``DOLT_RESET('--hard', sf_<id>)``

The control plane calls ``on_snapshot``/``on_restore`` explicitly at each
StateFork checkpoint (see ``statefork.py``); the CLI ``DoltController`` is *not*
used, because running CLI write commands against a live server would fight its
in-memory working set.

Dependency-free beyond PyMySQL so it can be unit tested against a real ephemeral
``dolt sql-server`` without FastAPI or checkpoint-lite.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger("control_plane.DataTier")


class DoltServerDataTier:
    """External Dolt data tier served by a long-lived ``dolt sql-server``."""

    backend = "dolt_server"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 3306,
        database: str = "shopdata",
        user: str = "root",
        password: str = "",
        branch_prefix: str = "sf_",
        working_branch: str = "main",
    ) -> None:
        self.host = host
        self.port = int(port)
        self.database = database
        self.user = user
        self.password = password
        self.branch_prefix = branch_prefix
        self.working_branch = working_branch

    # ---- connection helpers ---------------------------------------------- #
    def _connect(self):
        import pymysql

        return pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
        )

    def _query(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return list(cur.fetchall() or [])
        finally:
            conn.close()

    def _exec(self, statements: list[tuple[str, tuple]]) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                for sql, args in statements:
                    cur.execute(sql, args)
        finally:
            conn.close()

    def branch_name(self, snapshot_id: str) -> str:
        return f"{self.branch_prefix}{snapshot_id}"

    # ---- readiness / working-branch ------------------------------------- #
    def prepare(self) -> None:
        """Connectivity check; schema + seed are owned by the CatalogStore."""
        self._query("SELECT 1 AS ok")

    def mark_clean(self) -> None:
        """Commit the freshly seeded working set and pin a stable ``clean`` ref.

        ``clean`` is the diff baseline the catalog editor compares the working
        set against ("what changed since the pristine catalog")."""
        self._exec(
            [
                ("CALL DOLT_ADD('-A')", ()),
                ("CALL DOLT_COMMIT('-m', %s, '--allow-empty')", ("seed: pristine catalog",)),
                ("CALL DOLT_BRANCH('-f', 'clean', 'HEAD')", ()),
            ]
        )

    def _has_clean(self) -> bool:
        return bool(self._query("SELECT name FROM dolt_branches WHERE name = 'clean'"))

    def reset_to_clean(self) -> None:
        """Roll the working set back to the pristine ``clean`` baseline (or pin
        one if it does not exist yet). Used on workspace reset / re-selection."""
        if self._has_clean():
            self._exec(
                [
                    ("CALL DOLT_CHECKOUT(%s)", (self.working_branch,)),
                    ("CALL DOLT_RESET('--hard', 'clean')", ()),
                ]
            )
        else:
            self.mark_clean()

    # ---- read-side summary / fingerprint -------------------------------- #
    def _tables(self) -> list[str]:
        return [
            row["name"]
            for row in self._query(
                "SELECT table_name AS name FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' "
                "ORDER BY table_name"
            )
        ]

    def _columns(self, table: str) -> list[str]:
        return [
            row["col"]
            for row in self._query(
                "SELECT column_name AS col FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = %s "
                "ORDER BY ordinal_position",
                (table,),
            )
        ]

    def summary(self) -> dict[str, Any]:
        tables = self._tables()
        counts: dict[str, int] = {}
        fingerprints: dict[str, str] = {}
        for table in tables:
            counts[table] = int(
                next(iter(self._query(f"SELECT COUNT(*) AS c FROM `{table}`")[0].values()))
            )
            columns = self._columns(table)
            order_by = ", ".join(f"`{c}`" for c in columns) or "1"
            hasher = hashlib.sha256()
            for row in self._query(f"SELECT * FROM `{table}` ORDER BY {order_by}"):
                hasher.update(repr(tuple(row.get(c) for c in columns)).encode("utf-8"))
                hasher.update(b"\n")
            fingerprints[table] = hasher.hexdigest()
        return {"tables": tables, "counts": counts, "fingerprints": fingerprints}

    def fingerprint(self) -> str:
        summary = self.summary()
        payload = {"counts": summary["counts"], "fingerprints": summary["fingerprints"]}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    # ---- versioning (lockstep with StateFork) --------------------------- #
    def on_snapshot(self, snapshot_id: str) -> None:
        branch = self.branch_name(snapshot_id)
        self._exec(
            [
                ("CALL DOLT_ADD('-A')", ()),
                (
                    "CALL DOLT_COMMIT('-m', %s, '--allow-empty')",
                    (f"StateFork snapshot {snapshot_id}",),
                ),
                ("CALL DOLT_BRANCH('-f', %s, 'HEAD')", (branch,)),
            ]
        )

    def on_restore(self, snapshot_id: str) -> None:
        branch = self.branch_name(snapshot_id)
        self._exec(
            [
                ("CALL DOLT_CHECKOUT(%s)", (self.working_branch,)),
                ("CALL DOLT_RESET('--hard', %s)", (branch,)),
            ]
        )

    def cleanup(self) -> None:
        """Prune the per-snapshot ``sf_*`` branches (best effort)."""
        try:
            branches = self._query(
                "SELECT name FROM dolt_branches WHERE name LIKE %s",
                (f"{self.branch_prefix}%",),
            )
            for row in branches:
                try:
                    self._exec([("CALL DOLT_BRANCH('-D', %s)", (row["name"],))])
                except Exception:
                    pass
        except Exception:
            pass
