"""Control-plane data tier strategy.

The workspace controller (``StateForkBackend``) historically assumed the data
tier is a SQLite file inside the StateFork checkpoint (architecture **B**): it
fingerprints and summarises that file directly. To support architecture **A**
— an *external* Dolt database branched by StateFork's ``DoltController`` — the
controller delegates the read-side data operations (fingerprint / summary) to a
``DataTier`` and asks it for the extra ``create_env_manager`` kwargs that make
the StateFork manager version Dolt in lockstep.

Only the **Dolt** tier lives here; when no tier is configured the controller
keeps its original SQLite-file code path untouched (zero behaviour change).

This module is dependency-free (stdlib + the ``dolt`` CLI) so it can be unit
tested without FastAPI or checkpoint-lite.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class DataTier(ABC):
    backend: str = "base"

    @abstractmethod
    def prepare(self) -> None:
        """Ensure the data tier is ready (idempotent)."""

    @abstractmethod
    def fingerprint(self) -> str:
        """A stable hash of the current data, for dirty detection."""

    @abstractmethod
    def summary(self) -> dict[str, Any]:
        """``{"tables": [...], "counts": {t: n}, "fingerprints": {t: hash}}``."""

    @abstractmethod
    def statefork_kwargs(self) -> dict[str, Any]:
        """Extra kwargs for ``create_env_manager`` (e.g. dolt_repo)."""

    def on_snapshot(self, snapshot_id: str) -> None:
        """Version the data at a StateFork snapshot.

        Default no-op: the CLI tier lets the StateFork manager drive Dolt via
        the ``dolt_repo`` kwarg. The server tier overrides this to commit +
        branch through the running sql-server.
        """

    def on_restore(self, snapshot_id: str) -> None:
        """Roll the data back to a snapshot. See ``on_snapshot``."""

    def cleanup(self) -> None:
        """Best-effort teardown. The StateFork manager prunes Dolt branches."""


class DoltDataTier(DataTier):
    """External Dolt data tier (architecture A).

    Reads the working set via the ``dolt`` CLI for fingerprint/summary, and
    hands ``dolt_repo`` to the StateFork factory so the manager's snapshot()/
    restore() commit + branch + reset the database in lockstep with the app
    checkpoint.
    """

    backend = "dolt"

    def __init__(
        self,
        repo_dir: Path,
        branch_prefix: str = "sf_",
        working_branch: str = "main",
        dolt_bin: str = "dolt",
    ) -> None:
        self.repo_dir = Path(repo_dir).resolve()
        self.branch_prefix = branch_prefix
        self.working_branch = working_branch
        self.dolt_bin = dolt_bin

    # ---- CLI helpers ----------------------------------------------------- #
    def _require_dolt(self) -> None:
        if shutil.which(self.dolt_bin) is None:
            raise RuntimeError(
                f"`{self.dolt_bin}` not found on PATH; Dolt data tier unavailable."
            )

    def _sql(self, query: str, rows: bool = False) -> list[dict[str, Any]]:
        self._require_dolt()
        args = [self.dolt_bin, "sql", "-q", query]
        if rows:
            args += ["-r", "json"]
        proc = subprocess.run(args, cwd=self.repo_dir, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"dolt sql failed (rc={proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}\nQuery: {query}"
            )
        if not rows:
            return []
        out = proc.stdout.strip()
        return json.loads(out).get("rows", []) if out else []

    def _scalar(self, query: str) -> Any:
        result = self._sql(query, rows=True)
        return next(iter(result[0].values())) if result else None

    # ---- DataTier API ---------------------------------------------------- #
    def prepare(self) -> None:
        """Ensure the repo exists. Seeding is owned by the app's init_db()."""
        self._require_dolt()
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        if not (self.repo_dir / ".dolt").is_dir():
            subprocess.run(
                [self.dolt_bin, "init", "--name", "ControlPlane",
                 "--email", "control-plane@local", "--initial-branch", self.working_branch],
                cwd=self.repo_dir, capture_output=True, text=True, check=True,
            )

    def _tables(self) -> list[str]:
        return [
            row["name"]
            for row in self._sql(
                "SELECT table_name AS name FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' "
                "ORDER BY table_name",
                rows=True,
            )
        ]

    def _columns(self, table: str) -> list[str]:
        return [
            row["col"]
            for row in self._sql(
                "SELECT column_name AS col FROM information_schema.columns "
                f"WHERE table_schema = DATABASE() AND table_name = '{table}' "
                "ORDER BY ordinal_position",
                rows=True,
            )
        ]

    def summary(self) -> dict[str, Any]:
        tables = self._tables()
        counts: dict[str, int] = {}
        fingerprints: dict[str, str] = {}
        for table in tables:
            counts[table] = int(self._scalar(f"SELECT COUNT(*) FROM `{table}`") or 0)
            columns = self._columns(table)
            order_by = ", ".join(f"`{c}`" for c in columns) or "1"
            hasher = hashlib.sha256()
            for row in self._sql(f"SELECT * FROM `{table}` ORDER BY {order_by}", rows=True):
                hasher.update(repr(tuple(row.get(c) for c in columns)).encode("utf-8"))
                hasher.update(b"\n")
            fingerprints[table] = hasher.hexdigest()
        return {"tables": tables, "counts": counts, "fingerprints": fingerprints}

    def fingerprint(self) -> str:
        summary = self.summary()
        payload = {"counts": summary["counts"], "fingerprints": summary["fingerprints"]}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def statefork_kwargs(self) -> dict[str, Any]:
        return {
            "dolt_repo": str(self.repo_dir),
            "dolt_branch_prefix": self.branch_prefix,
            "dolt_working_branch": self.working_branch,
            "dolt_bin": self.dolt_bin,
        }


class DoltServerDataTier(DataTier):
    """External Dolt data tier served by a long-lived ``dolt sql-server``.

    Reads (summary/fingerprint) and versioning (snapshot/restore) both go over
    the MySQL protocol, so there is no per-call ``dolt`` process spawn and no CLI
    contention with the running server. Versioning uses Dolt's SQL procedures:

    - ``on_snapshot(id)``: ``DOLT_ADD('-A')`` + ``DOLT_COMMIT`` + ``DOLT_BRANCH``
    - ``on_restore(id)``: ``DOLT_CHECKOUT(working)`` + ``DOLT_RESET('--hard', sf_<id>)``

    ``statefork_kwargs()`` is empty: the StateFork manager must NOT also drive
    the CLI ``DoltController`` (that would fight the server), so the control plane
    calls ``on_snapshot``/``on_restore`` explicitly at each checkpoint.
    """

    backend = "dolt_server"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 3306,
        database: str = "inventory",
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

    def _connect(self):
        import pymysql
        return pymysql.connect(
            host=self.host, port=self.port, user=self.user, password=self.password,
            database=self.database, autocommit=True,
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

    def prepare(self) -> None:
        # Connectivity + schema are owned by the app's init_db(); just check.
        self._query("SELECT 1 AS ok")

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

    def statefork_kwargs(self) -> dict[str, Any]:
        return {}  # versioning is driven by on_snapshot/on_restore, not the CLI

    def on_snapshot(self, snapshot_id: str) -> None:
        branch = self.branch_name(snapshot_id)
        self._exec([
            ("CALL DOLT_ADD('-A')", ()),
            ("CALL DOLT_COMMIT('-m', %s, '--allow-empty')",
             (f"StateFork snapshot {snapshot_id}",)),
            ("CALL DOLT_BRANCH('-f', %s, 'HEAD')", (branch,)),
        ])

    def on_restore(self, snapshot_id: str) -> None:
        branch = self.branch_name(snapshot_id)
        self._exec([
            ("CALL DOLT_CHECKOUT(%s)", (self.working_branch,)),
            ("CALL DOLT_RESET('--hard', %s)", (branch,)),
        ])

    def cleanup(self) -> None:
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


def build_data_tier(
    backend: str,
    *,
    dolt_dir: Path | None = None,
    dolt_bin: str = "dolt",
    server_params: dict[str, Any] | None = None,
) -> DataTier | None:
    """Return a configured data tier, or None for the default SQLite-file path."""
    backend = (backend or "sqlite").lower()
    if backend == "sqlite":
        return None
    if backend == "dolt":
        if dolt_dir is None:
            raise ValueError("Dolt data tier requires dolt_dir")
        return DoltDataTier(Path(dolt_dir), dolt_bin=dolt_bin)
    if backend == "dolt_server":
        params = dict(server_params or {})
        return DoltServerDataTier(**params)
    raise ValueError(f"Unknown data backend: {backend!r}")
