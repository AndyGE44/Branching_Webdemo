from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from agent_safe_demo.app_plane.inventory_service.store import (
    InsufficientStock,
    InventoryStore,
    UnknownPart,
    create_store,
)

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = Path(os.getenv("DEMO_PROJECT_ROOT", BASE_DIR.parents[3]))
DB_PATH = Path(os.getenv("DEMO_INVENTORY_DB_PATH", PROJECT_ROOT / "demo_inventory.db"))

# Lazily-built data tier (SQLite by default, Dolt when DEMO_INVENTORY_DB_BACKEND=dolt).
# Lazy so merely importing this module (e.g. the control plane reading DB_PATH)
# has no side effects on the configured backend.
_store: InventoryStore | None = None


def get_store() -> InventoryStore:
    global _store
    if _store is None:
        _store = create_store(sqlite_path=DB_PATH)
    return _store


def init_db() -> None:
    get_store().init()


_ballast: bytearray | None = None


def _alloc_ballast() -> None:
    """Hold a fixed resident working set so the app-tier CRIU checkpoint has a
    realistic memory footprint to capture (build-mode benchmark knob). Touches
    every page so the bytes are resident (RSS), not lazily mapped. No-op when
    DEMO_INVENTORY_BALLAST_MB is unset/0."""
    global _ballast
    mb = int(os.getenv("DEMO_INVENTORY_BALLAST_MB", "0") or "0")
    if mb > 0:
        buf = bytearray(mb * 1024 * 1024)
        for i in range(0, len(buf), 4096):
            buf[i] = 1
        _ballast = buf


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    _alloc_ballast()
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


@app.post("/api/admin/drain-connections")
def drain_connections() -> dict:
    """Close any pooled DB connections so a CRIU checkpoint captures no open
    sockets to the external server. Called by the control plane before a
    checkpoint_exec snapshot; no-op for the SQLite / CLI backends."""
    drain = getattr(get_store(), "close_pool", None)
    if callable(drain):
        drain()
        return {"drained": True}
    return {"drained": False}


class ReserveRequest(BaseModel):
    part_id: str
    quantity: int = Field(gt=0)
    actor: str = "user"


class InventoryActionRequest(BaseModel):
    part_id: str
    quantity: int = Field(gt=0)
    actor: str = "user"


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


def _map_inventory_error(error: Exception) -> HTTPException:
    if isinstance(error, UnknownPart):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, InsufficientStock):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=500, detail=str(error))


@app.get("/api/inventory")
def inventory() -> dict:
    return {"items": get_store().inventory_items()}


@app.post("/api/inventory/buy")
def buy_stock(payload: InventoryActionRequest) -> dict:
    try:
        part = get_store().buy(payload.part_id, payload.quantity, payload.actor)
    except (UnknownPart, InsufficientStock) as error:
        raise _map_inventory_error(error) from error
    return {"status": "bought", "part": part}


@app.post("/api/inventory/sell")
def sell_stock(payload: InventoryActionRequest) -> dict:
    try:
        part = get_store().sell(payload.part_id, payload.quantity, payload.actor)
    except (UnknownPart, InsufficientStock) as error:
        raise _map_inventory_error(error) from error
    return {"status": "sold", "part": part}


@app.post("/api/reservations")
def reserve_stock(payload: ReserveRequest) -> dict:
    try:
        return get_store().reserve(payload.part_id, payload.quantity, payload.actor)
    except (UnknownPart, InsufficientStock) as error:
        raise _map_inventory_error(error) from error


@app.get("/api/state")
def state() -> dict:
    store = get_store()
    payload = store.state()
    return {
        "runtime": {"db_path": store.location_label, "backend": store.backend},
        **payload,
    }


@app.post("/api/reset")
def reset() -> dict:
    get_store().reset()
    return {"status": "reset"}
