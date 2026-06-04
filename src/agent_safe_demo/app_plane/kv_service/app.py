from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import AsyncIterator, Iterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = Path(os.getenv("DEMO_PROJECT_ROOT", BASE_DIR.parents[3]))
DB_PATH = Path(os.getenv("DEMO_KV_DB_PATH", PROJECT_ROOT / "demo_kv.db"))


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(
    title="Demo KV Service",
    description="A tiny file-backed key-value service. It does not know about StateFork.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SetValueRequest(BaseModel):
    value: str = Field(min_length=1, max_length=500)
    actor: str = "user"


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def rows(cursor: sqlite3.Cursor) -> list[dict]:
    return [dict(row) for row in cursor.fetchall()]


def audit(conn: sqlite3.Connection, actor: str, action: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO audit_log(actor, action, detail) VALUES (?, ?, ?)",
        (actor, action, detail),
    )


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kv_entries (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
        if not conn.execute("SELECT COUNT(*) AS count FROM kv_entries").fetchone()["count"]:
            conn.executemany(
                "INSERT INTO kv_entries(key, value) VALUES (?, ?)",
                [
                    ("mode", "demo"),
                    ("owner", "operations"),
                    ("status", "ready"),
                ],
            )
        if not conn.execute("SELECT COUNT(*) AS count FROM audit_log").fetchone()["count"]:
            audit(conn, "system", "seed", "Loaded sample key-value data")


@app.get("/", response_class=HTMLResponse)
def root() -> str:
    return """
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>KV Runtime</title>
        <style>
          body { margin: 0; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background: #f7f8f6; color: #18211c; }
          main { padding: 18px; display: grid; gap: 14px; }
          h1 { font-size: 20px; margin: 0; }
          p { margin: 4px 0 0; color: #5d6a62; }
          form { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; background: white; padding: 12px; border: 1px solid #d8dfd9; }
          label { display: grid; gap: 4px; font-size: 12px; color: #5d6a62; }
          input { padding: 8px; border: 1px solid #c8d0ca; border-radius: 6px; }
          button { border: 1px solid #174938; background: #174938; color: white; padding: 8px 10px; border-radius: 6px; cursor: pointer; }
          table { width: 100%; border-collapse: collapse; background: white; border: 1px solid #d8dfd9; }
          th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid #edf0ed; font-size: 13px; }
          th { background: #eef3ef; color: #425047; }
        </style>
      </head>
      <body>
        <main>
          <header><h1>KV Runtime</h1><p>Script-launched app running inside the StateFork-managed workspace.</p></header>
          <form id="entryForm">
            <label>Key<input name="key" required value="status" /></label>
            <label>Value<input name="value" required value="updated" /></label>
            <button>Set</button>
          </form>
          <table><thead><tr><th>Key</th><th>Value</th><th>Updated</th></tr></thead><tbody id="entries"></tbody></table>
        </main>
        <script>
          const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[char]));
          async function request(path, options = {}) {
            const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || `Request failed: ${response.status}`);
            return data;
          }
          async function refresh() {
            const state = await request('api/state');
            document.querySelector('#entries').innerHTML = state.entries.map((entry) => `<tr><td>${esc(entry.key)}</td><td>${esc(entry.value)}</td><td>${esc(entry.updated_at)}</td></tr>`).join('');
          }
          document.querySelector('#entryForm').addEventListener('submit', async (event) => {
            event.preventDefault();
            const form = new FormData(event.currentTarget);
            await request(`api/kv/${encodeURIComponent(form.get('key'))}`, { method: 'POST', body: JSON.stringify({ value: form.get('value'), actor: 'user' }) });
            await refresh();
          });
          refresh();
        </script>
      </body>
    </html>
    """


@app.get("/api/kv")
def list_entries() -> dict:
    with db() as conn:
        return {"entries": rows(conn.execute("SELECT * FROM kv_entries ORDER BY key"))}


@app.post("/api/kv/{key}")
def set_entry(key: str, payload: SetValueRequest) -> dict:
    clean_key = key.strip()
    if not clean_key:
        raise HTTPException(status_code=422, detail="Key cannot be blank")
    with db() as conn:
        conn.execute(
            """
            INSERT INTO kv_entries(key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (clean_key, payload.value),
        )
        audit(conn, payload.actor, "set", f"Set {clean_key}")
        return {"status": "set", "entry": dict(conn.execute("SELECT * FROM kv_entries WHERE key = ?", (clean_key,)).fetchone())}


@app.get("/api/state")
def state() -> dict:
    with db() as conn:
        entries = rows(conn.execute("SELECT * FROM kv_entries ORDER BY key"))
        audit_log = rows(conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 25"))
        return {
            "runtime": {"db_path": str(DB_PATH)},
            "summary": {
                "entries": len(entries),
                "audit_events": len(audit_log),
            },
            "entries": entries,
            "audit_log": audit_log,
        }


@app.post("/api/reset")
def reset() -> dict:
    if DB_PATH.exists():
        DB_PATH.unlink()
    init_db()
    return {"status": "reset"}
