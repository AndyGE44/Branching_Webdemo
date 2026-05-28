from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: str | Path) -> None:
    with connect(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS counter_state(id INTEGER PRIMARY KEY CHECK (id = 1), value INTEGER NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL, amount INTEGER NOT NULL, value_after INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("INSERT OR IGNORE INTO counter_state(id, value) VALUES (1, 0)")


def read_state(path: str | Path) -> dict[str, Any]:
    init_db(path)
    with connect(path) as conn:
        counter = conn.execute("SELECT value FROM counter_state WHERE id = 1").fetchone()
        audit = conn.execute("SELECT id, actor, action, amount, value_after, created_at FROM audit_log ORDER BY id").fetchall()
    return {"value": int(counter["value"]), "audit_log": [dict(row) for row in audit]}


def apply_delta(path: str | Path, amount: int, actor: str, action: str) -> dict[str, Any]:
    init_db(path)
    with connect(path) as conn:
        current = conn.execute("SELECT value FROM counter_state WHERE id = 1").fetchone()
        value_after = int(current["value"]) + amount
        conn.execute("UPDATE counter_state SET value = ? WHERE id = 1", (value_after,))
        conn.execute("INSERT INTO audit_log(actor, action, amount, value_after) VALUES (?, ?, ?, ?)", (actor, action, amount, value_after))
    return read_state(path)


def reset_db(path: str | Path) -> dict[str, Any]:
    db_path = Path(path)
    if db_path.exists():
        db_path.unlink()
    init_db(db_path)
    return read_state(db_path)


def diff_db(main_path: str | Path, branch_path: str | Path) -> dict[str, Any]:
    main = read_state(main_path)
    branch = read_state(branch_path)
    return {"main": main, "branch": branch, "value_delta": branch["value"] - main["value"], "audit_delta": branch["audit_log"][len(main["audit_log"]):]}


def copy_db(source_path: str | Path, target_path: str | Path) -> None:
    source_path = Path(source_path)
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source_path) as source, sqlite3.connect(target_path) as target:
        source.backup(target)
