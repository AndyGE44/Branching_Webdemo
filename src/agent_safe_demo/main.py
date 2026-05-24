from __future__ import annotations

import base64
import json
import sqlite3
from contextlib import asynccontextmanager, contextmanager
import os
from pathlib import Path
import secrets
from typing import AsyncIterator, Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent_safe_demo.branching import (
    BranchError,
    CheckpointLiteBackend,
    LocalCopyBackend,
    StateForkBackend,
)

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parents[1]
DB_PATH = Path(os.getenv("TOY_INVENTORY_DB_PATH", PROJECT_ROOT / "toy_inventory.db"))
STATIC_DIR = BASE_DIR / "static"
BRANCH_ID = os.getenv("TOY_INVENTORY_BRANCH_ID")
DEMO_AUTH_USER = os.getenv("TOY_DEMO_AUTH_USER", "demo")
DEMO_AUTH_PASSWORD = os.getenv("TOY_DEMO_AUTH_PASSWORD")
DEMO_AUTH_REALM = os.getenv("TOY_DEMO_AUTH_REALM", "Agent-Safe Demo")

def create_branch_backend() -> LocalCopyBackend | CheckpointLiteBackend | StateForkBackend:
    backend = os.getenv("TOY_BRANCH_BACKEND", "local-copy")
    if backend == "checkpoint-lite":
        return CheckpointLiteBackend(
            PROJECT_ROOT,
            DB_PATH,
            checkpoint_lite_bin=os.getenv("CHECKPOINT_LITE_BIN", "./checkpoint-lite"),
            checkpoint_sessions_dir=os.getenv(
                "TOY_CHECKPOINT_SESSIONS_DIR",
                "/tmp/checkpoint-sessions",
            ),
            host=os.getenv("TOY_BRANCH_HOST", "127.0.0.1"),
            port_start=int(os.getenv("TOY_BRANCH_PORT_START", "8200")),
            use_sudo=os.getenv("TOY_CHECKPOINT_USE_SUDO", "1") != "0",
        )
    if backend == "statefork":
        return StateForkBackend(
            PROJECT_ROOT,
            DB_PATH,
            statefork_root=Path(
                os.getenv("TOY_STATEFORK_ROOT", PROJECT_ROOT.parent / "StateFork")
            ),
            statefork_method=os.getenv("TOY_STATEFORK_METHOD", "ckpt_build"),
            statefork_cwd=Path(os.getenv("TOY_STATEFORK_CWD", str(PROJECT_ROOT))),
            statefork_kwargs=json.loads(os.getenv("TOY_STATEFORK_KWARGS", "{}")),
            host=os.getenv("TOY_BRANCH_HOST", "127.0.0.1"),
            port_start=int(os.getenv("TOY_BRANCH_PORT_START", "8300")),
        )
    return LocalCopyBackend(PROJECT_ROOT, DB_PATH)


branch_backend = create_branch_backend()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(
    title="Agent-Safe Toy Inventory",
    description="A tiny inventory workflow for branching-state web service demos.",
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
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def demo_auth_enabled() -> bool:
    return bool(DEMO_AUTH_PASSWORD) and not BRANCH_ID


def unauthorized_response() -> JSONResponse:
    return JSONResponse(
        {"detail": "Authentication required"},
        status_code=401,
        headers={"WWW-Authenticate": f'Basic realm="{DEMO_AUTH_REALM}"'},
    )


def valid_basic_auth(authorization: str | None) -> bool:
    if not authorization or not authorization.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(authorization.removeprefix("Basic ")).decode()
    except (UnicodeDecodeError, ValueError):
        return False
    username, separator, password = decoded.partition(":")
    if not separator:
        return False
    return secrets.compare_digest(username, DEMO_AUTH_USER) and secrets.compare_digest(
        password,
        DEMO_AUTH_PASSWORD or "",
    )


@app.middleware("http")
async def require_demo_password(request: Request, call_next):
    if demo_auth_enabled() and not valid_basic_auth(request.headers.get("authorization")):
        return unauthorized_response()
    return await call_next(request)


class ReserveRequest(BaseModel):
    part_id: str
    quantity: int = Field(gt=0)
    actor: str = "user"


class InventoryActionRequest(BaseModel):
    part_id: str
    quantity: int = Field(gt=0)
    actor: str = "user"


class BaseCheckpointRequest(BaseModel):
    label: str | None = Field(default=None, max_length=80)


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

        count = conn.execute("SELECT COUNT(*) AS count FROM parts").fetchone()["count"]
        if count:
            return

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
        audit(conn, "system", "seed", "Loaded sample inventory data")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/inventory")
def inventory() -> dict:
    with db() as conn:
        items = rows(
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
    return {"items": items}


@app.post("/api/inventory/buy")
def buy_stock(payload: InventoryActionRequest) -> dict:
    with db() as conn:
        ensure_part(conn, payload.part_id)
        conn.execute(
            "UPDATE parts SET on_hand = on_hand + ? WHERE id = ?",
            (payload.quantity, payload.part_id),
        )
        audit(
            conn,
            payload.actor,
            "buy",
            f"Bought {payload.quantity} units of {payload.part_id}",
        )
        return {
            "status": "bought",
            "part": inventory_item(conn, payload.part_id),
        }


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
        audit(
            conn,
            payload.actor,
            "sell",
            f"Sold {payload.quantity} units of {payload.part_id}",
        )
        return {
            "status": "sold",
            "part": inventory_item(conn, payload.part_id),
        }


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
        audit(
            conn,
            payload.actor,
            "reserve",
            f"Reserved {payload.quantity} units of {payload.part_id}",
        )
        return {"reservation_id": cursor.lastrowid, "status": "active"}


@app.get("/api/state")
def state() -> dict:
    with db() as conn:
        return {
            "runtime": {
                "branch_id": BRANCH_ID,
                "db_path": str(DB_PATH),
            },
            "inventory": rows(
                conn.execute(
                    """
                    SELECT
                        p.id,
                        p.name,
                        p.on_hand,
                        p.on_hand - COALESCE(SUM(r.quantity), 0) AS available,
                        COALESCE(SUM(r.quantity), 0) AS reserved
                    FROM parts p
                    LEFT JOIN reservations r ON r.part_id = p.id AND r.status = 'active'
                    GROUP BY p.id
                    ORDER BY p.id
                    """
                )
            ),
            "reservations": rows(conn.execute("SELECT * FROM reservations ORDER BY id DESC")),
            "audit_log": rows(conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 25")),
        }


@app.post("/api/reset")
def reset() -> dict:
    cleanup = branch_backend.reset()
    if DB_PATH.exists():
        DB_PATH.unlink()
    init_db()
    return {"status": "reset", "cleanup": cleanup}


def branch_error(error: BranchError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(error))


@app.get("/api/backend")
def backend_status() -> dict:
    return branch_backend.status()


@app.get("/api/bases")
def list_bases() -> dict:
    return {"backend": branch_backend.name, "bases": branch_backend.list_bases()}


@app.post("/api/bases")
def create_base(payload: BaseCheckpointRequest | None = None) -> dict:
    try:
        label = payload.label if payload else None
        return {"base": branch_backend.create_base(label=label)}
    except BranchError as error:
        raise branch_error(error) from error


@app.post("/api/bases/{base_id}/branches")
def create_branch_from_base(base_id: str) -> dict:
    try:
        return {"branch": branch_backend.create_branch(base_id=base_id)}
    except BranchError as error:
        raise branch_error(error) from error


@app.delete("/api/bases/{base_id}")
def delete_base(base_id: str) -> dict:
    try:
        return branch_backend.delete_base(base_id)
    except BranchError as error:
        raise branch_error(error) from error


@app.get("/api/branches")
def list_branches() -> dict:
    return {"backend": branch_backend.name, "branches": branch_backend.list_branches()}


@app.post("/api/branches")
def create_branch() -> dict:
    try:
        return {"branch": branch_backend.create_branch()}
    except BranchError as error:
        raise branch_error(error) from error


@app.post("/api/branches/{branch_id}/run-agent-demo")
def run_branch_agent_demo(branch_id: str) -> dict:
    try:
        return branch_backend.run_agent_demo(branch_id)
    except BranchError as error:
        raise branch_error(error) from error


@app.get("/api/branches/{branch_id}/diff")
def branch_diff(branch_id: str) -> dict:
    try:
        return branch_backend.diff(branch_id)
    except BranchError as error:
        raise branch_error(error) from error


@app.post("/api/branches/{branch_id}/commit")
def commit_branch(branch_id: str) -> dict:
    try:
        return branch_backend.commit(branch_id)
    except BranchError as error:
        raise branch_error(error) from error


@app.post("/api/branches/{branch_id}/discard")
def discard_branch(branch_id: str) -> dict:
    try:
        return branch_backend.discard(branch_id)
    except BranchError as error:
        raise branch_error(error) from error
