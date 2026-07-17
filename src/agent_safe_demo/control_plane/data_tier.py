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

    def merge_into_working(
        self,
        base_ref: str,
        merge_refs: list[str],
        resolutions: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Reset the working branch to ``base_ref``'s snapshot branch, then
        ``DOLT_MERGE`` each of ``merge_refs`` in turn (Dolt's cell-level 3-way
        merge). Returns ``{"conflicts": [...], "resolved": [...]}``.

        A conflict arises when two refs wrote the same cell. Each conflict entry
        carries the full ``base``/``ours``/``theirs`` row (from Dolt's
        ``dolt_conflicts_variant_state``) plus ``theirs_ref`` — the ref being
        merged when the clash happened; ``ours`` is the working set (the base
        plus any ref already merged).

        ``resolutions`` maps ``variant_id`` to a decision:

        - ``{"take": <ref>}`` — keep that ref's side of the row (``theirs_ref``
          → their row is written; the other ref → the working set already holds
          it, per the row-level flow Dolt documents for SQL sessions).
        - ``{"set": {field: value}}`` — write custom values (editable fields
          only), keeping the rest of the working row.

        Fully resolved conflicts are cleared from ``dolt_conflicts_*`` and the
        merge is committed, so versioning can proceed. Any conflict *without* a
        resolution aborts the whole merge and rolls the working set back to
        ``base_ref``, keeping the data consistent with the (restored) app base.

        ``@@autocommit`` is disabled for the session so Dolt holds conflicts in
        ``dolt_conflicts_*`` for inspection instead of erroring the statement.
        """
        import pymysql

        resolutions = resolutions or {}
        base_branch = self.branch_name(base_ref)
        conn = pymysql.connect(
            host=self.host, port=self.port, user=self.user, password=self.password,
            database=self.database, autocommit=True, cursorclass=pymysql.cursors.DictCursor,
        )
        resolved: list[str] = []
        try:
            with conn.cursor() as cur:
                cur.execute("SET @@autocommit = 0")

                def proc(sql, args=()):
                    cur.execute(sql, args)
                    rows = cur.fetchall()
                    while cur.nextset():
                        pass
                    return rows

                def abort_to_base():
                    proc("CALL DOLT_MERGE('--abort')")
                    proc("CALL DOLT_RESET('--hard', %s)", (base_branch,))
                    conn.commit()

                proc("CALL DOLT_CHECKOUT(%s)", (self.working_branch,))
                proc("CALL DOLT_RESET('--hard', %s)", (base_branch,))
                conn.commit()
                for ref in merge_refs:
                    result = proc("CALL DOLT_MERGE(%s)", (self.branch_name(ref),))
                    conflict_count = int((result[0].get("conflicts") if result else 0) or 0)
                    if conflict_count == 0:
                        conn.commit()
                        continue
                    cur.execute("SELECT * FROM dolt_conflicts_variant_state")
                    pending = [_split_conflict_row(dict(row)) for row in cur.fetchall()]
                    if any(c["variant_id"] not in resolutions for c in pending):
                        report = [{**c, "theirs_ref": ref} for c in pending]
                        abort_to_base()
                        return {"conflicts": report, "resolved": []}
                    for conflict in pending:
                        _apply_conflict_resolution(
                            cur, conflict, resolutions[conflict["variant_id"]], ref
                        )
                        resolved.append(conflict["variant_id"])
                    cur.execute("DELETE FROM dolt_conflicts_variant_state")
                    remaining = proc(
                        "SELECT COALESCE(SUM(num_conflicts), 0) AS n FROM dolt_conflicts"
                    )
                    if int((remaining[0].get("n") if remaining else 0) or 0) != 0:
                        abort_to_base()
                        raise RuntimeError(
                            "conflicts remained after applying resolutions; merge aborted"
                        )
                    # Finalize this ref's merge so the next DOLT_MERGE starts
                    # from a committed state (the flow Dolt documents).
                    proc(
                        "CALL DOLT_COMMIT('-a', '-m', %s)",
                        (f"resolve merge of {self.branch_name(ref)}",),
                    )
                    conn.commit()
            return {"conflicts": [], "resolved": resolved}
        finally:
            conn.close()

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


# --------------------------------------------------------------------------- #
# merge-conflict helpers
# --------------------------------------------------------------------------- #

# Fields a {"set": ...} resolution may write — the same set the catalog editor
# can change (identity/display columns are never in conflict).
_RESOLVABLE_SET_FIELDS = ("price", "compare_at_price", "on_hand", "available")


def _split_conflict_row(row: dict[str, Any]) -> dict[str, Any]:
    """Split one ``dolt_conflicts_<table>`` row into base/ours/theirs dicts.

    The system table prefixes every real column with ``base_``/``our_``/
    ``their_`` (bookkeeping columns like ``from_root_ish`` and ``*_diff_type``
    have no ``base_`` twin, so keying off the ``base_`` prefix skips them).
    """
    columns = [key[len("base_"):] for key in row if key.startswith("base_")]
    variant_id = (
        row.get("our_variant_id") or row.get("their_variant_id") or row.get("base_variant_id")
    )
    return {
        "variant_id": str(variant_id),
        "base": {c: row.get(f"base_{c}") for c in columns},
        "ours": {c: row.get(f"our_{c}") for c in columns},
        "theirs": {c: row.get(f"their_{c}") for c in columns},
    }


def _apply_conflict_resolution(
    cur, conflict: dict[str, Any], decision: dict[str, Any], theirs_ref: str
) -> None:
    """Apply one variant's resolution to the working set (same open session).

    Clearing the marker row from ``dolt_conflicts_variant_state`` happens in
    bulk afterwards; this only writes the winning cell values.
    """
    variant_id = conflict["variant_id"]
    if not isinstance(decision, dict):
        raise ValueError(f"Invalid resolution for variant {variant_id}: {decision!r}")

    if "take" in decision:
        if str(decision["take"]) != str(theirs_ref):
            return  # the working set already holds the other (ours) side
        theirs = conflict["theirs"]
        if theirs.get("variant_id") is None:
            # Their side deleted the row (not producible by the catalog editor,
            # but cheap to honor).
            cur.execute("DELETE FROM variant_state WHERE variant_id = %s", (variant_id,))
            return
        columns = list(theirs)
        cur.execute(
            "INSERT INTO variant_state ({cols}) VALUES ({vals}) "
            "ON DUPLICATE KEY UPDATE {sets}".format(
                cols=", ".join(f"`{c}`" for c in columns),
                vals=", ".join(["%s"] * len(columns)),
                sets=", ".join(f"`{c}` = VALUES(`{c}`)" for c in columns if c != "variant_id"),
            ),
            tuple(theirs[c] for c in columns),
        )
        return

    custom = decision.get("set")
    if isinstance(custom, dict):
        fields = {k: v for k, v in custom.items() if k in _RESOLVABLE_SET_FIELDS}
        if not fields:
            raise ValueError(
                f"Resolution for variant {variant_id} sets no editable field "
                f"(allowed: {', '.join(_RESOLVABLE_SET_FIELDS)})"
            )
        cur.execute(
            "UPDATE variant_state SET {sets} WHERE variant_id = %s".format(
                sets=", ".join(f"`{k}` = %s" for k in fields)
            ),
            tuple(fields.values()) + (variant_id,),
        )
        return

    raise ValueError(
        f"Invalid resolution for variant {variant_id}: expected "
        '{"take": <snapshot>} or {"set": {field: value}}'
    )
