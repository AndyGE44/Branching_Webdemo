from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


class CommitStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS commits (
                    id TEXT PRIMARY KEY,
                    app_id TEXT NOT NULL,
                    parent_commit_id TEXT,
                    base_id TEXT NOT NULL,
                    branch_id TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    message TEXT NOT NULL,
                    author TEXT NOT NULL,
                    diff_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_commits_app_created
                ON commits (app_id, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_heads (
                    app_id TEXT PRIMARY KEY,
                    commit_id TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(commit_id) REFERENCES commits(id)
                )
                """
            )

    def create_commit(
        self,
        *,
        app_id: str,
        parent_commit_id: str | None,
        base_id: str,
        branch_id: str,
        checkpoint_id: str,
        label: str,
        message: str = "",
        author: str = "user",
        diff: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.init_db()
        commit = {
            "id": f"commit-{uuid.uuid4().hex[:8]}",
            "app_id": app_id,
            "parent_commit_id": parent_commit_id,
            "base_id": base_id,
            "branch_id": branch_id,
            "checkpoint_id": checkpoint_id,
            "label": label,
            "message": message,
            "author": author,
            "diff": diff or {},
            "created_at": time.time(),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO commits (
                    id, app_id, parent_commit_id, base_id, branch_id, checkpoint_id,
                    label, message, author, diff_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    commit["id"],
                    app_id,
                    parent_commit_id,
                    base_id,
                    branch_id,
                    checkpoint_id,
                    label,
                    message,
                    author,
                    json.dumps(commit["diff"], sort_keys=True),
                    commit["created_at"],
                ),
            )
            conn.execute(
                """
                INSERT INTO app_heads (app_id, commit_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(app_id) DO UPDATE SET
                    commit_id = excluded.commit_id,
                    updated_at = excluded.updated_at
                """,
                (app_id, commit["id"], commit["created_at"]),
            )
        return commit

    def list_commits(self, app_id: str, limit: int = 20) -> list[dict[str, Any]]:
        self.init_db()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM commits
                WHERE app_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (app_id, limit),
            ).fetchall()
        return [self._row_to_commit(row) for row in rows]

    def reset_app(self, app_id: str) -> None:
        """Drop all committed heads and history for an app (used by workspace
        reset, so a reset truly returns to a clean slate with no prior head)."""
        self.init_db()
        with self._connect() as conn:
            conn.execute("DELETE FROM app_heads WHERE app_id = ?", (app_id,))
            conn.execute("DELETE FROM commits WHERE app_id = ?", (app_id,))

    def app_head(self, app_id: str) -> dict[str, Any] | None:
        self.init_db()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT commits.*
                FROM app_heads
                JOIN commits ON commits.id = app_heads.commit_id
                WHERE app_heads.app_id = ?
                """,
                (app_id,),
            ).fetchone()
        return self._row_to_commit(row) if row else None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row_to_commit(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "app_id": row["app_id"],
            "parent_commit_id": row["parent_commit_id"],
            "base_id": row["base_id"],
            "branch_id": row["branch_id"],
            "checkpoint_id": row["checkpoint_id"],
            "label": row["label"],
            "message": row["message"],
            "author": row["author"],
            "diff": json.loads(row["diff_json"] or "{}"),
            "created_at": row["created_at"],
        }
