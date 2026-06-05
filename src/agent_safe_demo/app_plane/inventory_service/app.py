from __future__ import annotations

import os
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import AsyncIterator, Iterator
import sqlite3

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = Path(os.getenv("DEMO_PROJECT_ROOT", BASE_DIR.parents[3]))
DB_PATH = Path(os.getenv("DEMO_INVENTORY_DB_PATH", PROJECT_ROOT / "demo_inventory.db"))


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(
    title="Demo Inventory Service",
    description="A plain inventory service. It does not know about StateFork.",
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


class ReserveRequest(BaseModel):
    part_id: str
    quantity: int = Field(gt=0)
    actor: str = "user"


class InventoryActionRequest(BaseModel):
    part_id: str
    quantity: int = Field(gt=0)
    actor: str = "user"


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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


def ensure_part(conn: sqlite3.Connection, part_id: str) -> sqlite3.Row:
    part = conn.execute("SELECT * FROM parts WHERE id = ?", (part_id,)).fetchone()
    if part is None:
        raise HTTPException(status_code=404, detail=f"Unknown part: {part_id}")
    return part


def available_quantity(conn: sqlite3.Connection, part_id: str) -> int:
    row = conn.execute(
        """
        SELECT p.on_hand - COALESCE(SUM(r.quantity), 0) AS available
        FROM parts p
        LEFT JOIN reservations r ON r.part_id = p.id AND r.status = 'active'
        WHERE p.id = ?
        GROUP BY p.id
        """,
        (part_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown part: {part_id}")
    return int(row["available"])


def inventory_item(conn: sqlite3.Connection, part_id: str) -> dict:
    row = conn.execute(
        """
        SELECT
            p.id,
            p.name,
            p.location,
            p.on_hand,
            p.reorder_point,
            p.on_hand - COALESCE(SUM(r.quantity), 0) AS available,
            COALESCE(SUM(r.quantity), 0) AS reserved
        FROM parts p
        LEFT JOIN reservations r ON r.part_id = p.id AND r.status = 'active'
        WHERE p.id = ?
        GROUP BY p.id
        """,
        (part_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown part: {part_id}")
    return dict(row)


def inventory_items(conn: sqlite3.Connection) -> list[dict]:
    return rows(
        conn.execute(
            """
            SELECT
                p.id,
                p.name,
                p.location,
                p.on_hand,
                p.reorder_point,
                p.on_hand - COALESCE(SUM(r.quantity), 0) AS available,
                COALESCE(SUM(r.quantity), 0) AS reserved
            FROM parts p
            LEFT JOIN reservations r ON r.part_id = p.id AND r.status = 'active'
            GROUP BY p.id
            ORDER BY p.id
            """
        )
    )


def init_db() -> None:
    with db() as conn:
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
        part_count = conn.execute("SELECT COUNT(*) AS count FROM parts").fetchone()["count"]
        if not part_count:
            conn.executemany(
                """
                INSERT INTO parts(id, name, location, on_hand, reorder_point)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    ("MCU-100", "Control board", "Aisle 1 / Bin 02", 8, 4),
                    ("MCU-ALT", "Backup control board", "Aisle 1 / Bin 08", 4, 2),
                    ("SENSOR-9", "Temperature sensor", "Aisle 3 / Bin 11", 2, 5),
                    ("CASE-42", "Aluminum enclosure", "Aisle 4 / Bin 01", 12, 4),
                    ("WIRE-RED", "Red harness wire", "Aisle 2 / Bin 05", 50, 10),
                ],
            )
        if not conn.execute("SELECT COUNT(*) AS count FROM audit_log").fetchone()["count"]:
            audit(conn, "system", "seed", "Loaded sample inventory data")


@app.get("/", response_class=HTMLResponse)
def root() -> str:
    return """
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Inventory Runtime</title>
        <style>
          body { margin: 0; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background: #f6faff; color: #102033; }
          main { padding: 18px; display: grid; gap: 14px; }
          header { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
          h1 { font-size: 20px; margin: 0; }
          p { margin: 4px 0 0; color: #5e7089; }
          button { border: 1px solid #c8d9ee; background: #fff; padding: 8px 10px; border-radius: 6px; cursor: pointer; color: #102033; }
          button.primary { background: #2d62ad; color: white; border-color: #2d62ad; }
          table { width: 100%; border-collapse: collapse; background: white; border: 1px solid #c8d9ee; }
          th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid #e6eef8; font-size: 13px; }
          th { background: #e7f1ff; color: #173f7a; }
          form { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; background: white; padding: 12px; border: 1px solid #c8d9ee; }
          label { display: grid; gap: 4px; font-size: 12px; color: #5e7089; }
          input, select { padding: 8px; border: 1px solid #c8d9ee; border-radius: 6px; color: #102033; }
          .result { color: #173f7a; font-size: 13px; }
        </style>
      </head>
      <body>
        <main>
          <header>
            <div>
              <h1>Inventory Runtime</h1>
              <p>Plain app UI running inside the StateFork-managed environment.</p>
            </div>
            <button id="refreshBtn">Refresh</button>
          </header>
          <form id="actionForm">
            <label>Part<select name="part_id" id="partSelect"></select></label>
            <label>Quantity<input name="quantity" type="number" min="1" value="1" /></label>
            <label>Action<select name="action"><option value="reserve">Reserve</option><option value="buy">Buy</option><option value="sell">Sell</option></select></label>
            <button class="primary" type="submit">Apply</button>
            <span id="result" class="result">Ready</span>
          </form>
          <section><table><thead><tr><th>Part</th><th>Name</th><th>Location</th><th>On Hand</th><th>Available</th><th>Reserved</th><th>Reorder</th></tr></thead><tbody id="items"></tbody></table></section>
          <section><table><thead><tr><th>ID</th><th>Part</th><th>Qty</th><th>Status</th><th>Actor</th><th>Created</th></tr></thead><tbody id="reservations"></tbody></table></section>
        </main>
        <script>
          const itemsEl = document.querySelector('#items');
          const reservationsEl = document.querySelector('#reservations');
          const partSelect = document.querySelector('#partSelect');
          const resultEl = document.querySelector('#result');
          const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[char]));
          async function request(path, options = {}) {
            const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || `Request failed: ${response.status}`);
            return data;
          }
          function row(cells) { return `<tr>${cells.map((cell) => `<td>${esc(cell)}</td>`).join('')}</tr>`; }
          async function refresh() {
            const state = await request('api/state');
            itemsEl.innerHTML = state.inventory.map((item) => row([item.id, item.name, item.location, item.on_hand, item.available, item.reserved, item.reorder_point])).join('');
            reservationsEl.innerHTML = state.reservations.length ? state.reservations.map((r) => row([r.id, r.part_id, r.quantity, r.status, r.actor, r.created_at])).join('') : '<tr><td colspan="6">No reservations yet</td></tr>';
            partSelect.innerHTML = state.inventory.map((item) => `<option value="${esc(item.id)}">${esc(item.id)}</option>`).join('');
          }
          document.querySelector('#refreshBtn').addEventListener('click', refresh);
          document.querySelector('#actionForm').addEventListener('submit', async (event) => {
            event.preventDefault();
            const form = new FormData(event.currentTarget);
            const action = form.get('action');
            const body = JSON.stringify({ part_id: form.get('part_id'), quantity: Number(form.get('quantity')), actor: 'user' });
            const path = action === 'reserve' ? 'api/reservations' : `api/inventory/${action}`;
            try { await request(path, { method: 'POST', body }); resultEl.textContent = `${action} saved`; await refresh(); }
            catch (error) { resultEl.textContent = error.message; }
          });
          refresh().catch((error) => { resultEl.textContent = error.message; });
        </script>
      </body>
    </html>
    """


@app.get("/api/inventory")
def inventory() -> dict:
    with db() as conn:
        return {"items": inventory_items(conn)}


@app.post("/api/inventory/buy")
def buy_stock(payload: InventoryActionRequest) -> dict:
    with db() as conn:
        ensure_part(conn, payload.part_id)
        conn.execute(
            "UPDATE parts SET on_hand = on_hand + ? WHERE id = ?",
            (payload.quantity, payload.part_id),
        )
        audit(conn, payload.actor, "buy", f"Bought {payload.quantity} units of {payload.part_id}")
        return {"status": "bought", "part": inventory_item(conn, payload.part_id)}


@app.post("/api/inventory/sell")
def sell_stock(payload: InventoryActionRequest) -> dict:
    with db() as conn:
        part = ensure_part(conn, payload.part_id)
        available = available_quantity(conn, payload.part_id)
        if payload.quantity > available:
            raise HTTPException(
                status_code=409,
                detail=f"Only {available} units of {part['id']} are available to sell",
            )
        conn.execute(
            "UPDATE parts SET on_hand = on_hand - ? WHERE id = ?",
            (payload.quantity, payload.part_id),
        )
        audit(conn, payload.actor, "sell", f"Sold {payload.quantity} units of {payload.part_id}")
        return {"status": "sold", "part": inventory_item(conn, payload.part_id)}


@app.post("/api/reservations")
def reserve_stock(payload: ReserveRequest) -> dict:
    with db() as conn:
        part = ensure_part(conn, payload.part_id)
        available = available_quantity(conn, payload.part_id)
        if payload.quantity > available:
            raise HTTPException(
                status_code=409,
                detail=f"Only {available} units of {part['id']} are available",
            )
        cursor = conn.execute(
            """
            INSERT INTO reservations(part_id, quantity, status, actor)
            VALUES (?, ?, 'active', ?)
            """,
            (payload.part_id, payload.quantity, payload.actor),
        )
        audit(conn, payload.actor, "reserve", f"Reserved {payload.quantity} units of {payload.part_id}")
        return {"reservation_id": cursor.lastrowid, "status": "active"}


@app.get("/api/state")
def state() -> dict:
    with db() as conn:
        inventory_rows = inventory_items(conn)
        reservations = rows(conn.execute("SELECT * FROM reservations ORDER BY id DESC"))
        return {
            "runtime": {"db_path": str(DB_PATH)},
            "summary": {
                "items": len(inventory_rows),
                "reservations": len(reservations),
                "low_stock": sum(1 for item in inventory_rows if item["available"] < item["reorder_point"]),
            },
            "inventory": inventory_rows,
            "reservations": reservations,
            "audit_log": rows(conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 25")),
        }


@app.post("/api/reset")
def reset() -> dict:
    if DB_PATH.exists():
        DB_PATH.unlink()
    init_db()
    return {"status": "reset"}
