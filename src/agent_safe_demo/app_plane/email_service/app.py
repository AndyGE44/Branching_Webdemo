from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager, contextmanager
import os
from pathlib import Path
import secrets
from typing import AsyncIterator, Iterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = Path(os.getenv("DEMO_PROJECT_ROOT", BASE_DIR.parents[3]))
DB_PATH = Path(os.getenv("DEMO_MAILBOX_DB_PATH", PROJECT_ROOT / "demo_mailbox.db"))


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(
    title="Demo Email Service",
    description="A plain email service. It does not know about StateFork.",
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


class LabelMessageRequest(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    actor: str = "user"


class MoveMessageRequest(BaseModel):
    folder: str = Field(min_length=1, max_length=40)
    actor: str = "user"


class ReadMessageRequest(BaseModel):
    is_read: bool = True
    actor: str = "user"


class ActorRequest(BaseModel):
    actor: str = "user"


class CreateMessageRequest(BaseModel):
    id: str | None = Field(default=None, max_length=80)
    from_address: str = Field(min_length=1, max_length=254)
    to_address: str = Field(min_length=1, max_length=254)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    folder: str = "Inbox"
    is_read: bool = False
    priority: str = "normal"
    actor: str = "user"


class DraftRequest(BaseModel):
    source_message_id: str | None = None
    to_address: str = Field(min_length=1, max_length=254)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    created_by: str = "user"


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


def normalize_label(label: str) -> str:
    normalized = label.strip().lower()
    if not normalized:
        raise HTTPException(status_code=422, detail="Label cannot be blank")
    return normalized


def normalize_folder(folder: str) -> str:
    aliases = {
        "inbox": "Inbox",
        "archive": "Archive",
        "spam": "Spam",
    }
    normalized = aliases.get(folder.strip().lower())
    if normalized is None:
        raise HTTPException(
            status_code=422,
            detail="Folder must be one of Inbox, Archive, or Spam",
        )
    return normalized


def strip_required(value: str, field_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise HTTPException(status_code=422, detail=f"{field_name} cannot be blank")
    return stripped


def normalize_priority(priority: str) -> str:
    normalized = priority.strip().lower()
    if normalized not in {"urgent", "high", "normal", "low"}:
        raise HTTPException(
            status_code=422,
            detail="Priority must be one of urgent, high, normal, or low",
        )
    return normalized


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


def message_with_labels(conn: sqlite3.Connection, message_id: str) -> dict:
    row = conn.execute(
        """
        SELECT
            m.*,
            COALESCE(GROUP_CONCAT(ml.label), '') AS labels
        FROM messages m
        LEFT JOIN message_labels ml ON ml.message_id = m.id
        WHERE m.id = ?
        GROUP BY m.id
        """,
        (message_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown message: {message_id}")
    message = dict(row)
    message["labels"] = [label for label in message["labels"].split(",") if label]
    message["is_read"] = bool(message["is_read"])
    return message


def draft_row(conn: sqlite3.Connection, draft_id: int) -> dict:
    row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown draft: {draft_id}")
    return dict(row)


def message_rows(conn: sqlite3.Connection) -> list[dict]:
    messages = rows(
        conn.execute(
            """
            SELECT
                m.*,
                COALESCE(GROUP_CONCAT(ml.label), '') AS labels
            FROM messages m
            LEFT JOIN message_labels ml ON ml.message_id = m.id
            GROUP BY m.id
            ORDER BY
                CASE m.priority
                    WHEN 'urgent' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'normal' THEN 2
                    ELSE 3
                END,
                m.created_at DESC
            """
        )
    )
    for message in messages:
        message["labels"] = [label for label in message["labels"].split(",") if label]
        message["is_read"] = bool(message["is_read"])
    return messages


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

            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                from_address TEXT NOT NULL,
                to_address TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                folder TEXT NOT NULL,
                is_read INTEGER NOT NULL CHECK(is_read IN (0, 1)),
                priority TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS message_labels (
                message_id TEXT NOT NULL,
                label TEXT NOT NULL,
                PRIMARY KEY(message_id, label),
                FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_message_id TEXT,
                to_address TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(source_message_id) REFERENCES messages(id)
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

        message_count = conn.execute("SELECT COUNT(*) AS count FROM messages").fetchone()["count"]
        if not message_count:
            conn.executemany(
                """
                INSERT INTO messages(
                    id, from_address, to_address, subject, body, folder, is_read, priority
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "msg-1001",
                        "billing@northwind.example",
                        "ops@example.com",
                        "Invoice for April services",
                        "Attached is the April service invoice. Please confirm receipt.",
                        "Inbox",
                        0,
                        "high",
                    ),
                    (
                        "msg-1002",
                        "customer@acme.example",
                        "support@example.com",
                        "Urgent: shipment delay",
                        "Our replacement sensors have not arrived. Can you send an updated ETA?",
                        "Inbox",
                        0,
                        "urgent",
                    ),
                    (
                        "msg-1003",
                        "prizes@promo.example",
                        "ops@example.com",
                        "Win a free prize today",
                        "Click this suspicious link to claim an unrealistic prize.",
                        "Inbox",
                        0,
                        "low",
                    ),
                    (
                        "msg-1004",
                        "ci@example.com",
                        "dev@example.com",
                        "Weekly CI report",
                        "All scheduled builds passed. One flaky integration test was retried.",
                        "Inbox",
                        1,
                        "normal",
                    ),
                    (
                        "msg-1005",
                        "teammate@example.com",
                        "ops@example.com",
                        "Re: launch checklist",
                        "I archived the old checklist and left comments on the new one.",
                        "Archive",
                        1,
                        "normal",
                    ),
                ],
            )
            conn.executemany(
                "INSERT INTO message_labels(message_id, label) VALUES (?, ?)",
                [
                    ("msg-1001", "billing"),
                    ("msg-1002", "customer"),
                    ("msg-1002", "urgent"),
                    ("msg-1004", "engineering"),
                ],
            )
            conn.execute(
                """
                INSERT INTO drafts(source_message_id, to_address, subject, body, status, created_by)
                VALUES (?, ?, ?, ?, 'draft', 'user')
                """,
                (
                    "msg-1005",
                    "teammate@example.com",
                    "Re: launch checklist",
                    "Thanks. I will review the new checklist before the next demo.",
                ),
            )

        if not conn.execute("SELECT COUNT(*) AS count FROM audit_log").fetchone()["count"]:
            audit(conn, "system", "seed", "Loaded sample mailbox data")


@app.get("/", response_class=HTMLResponse)
def root() -> str:
    return """
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Email Runtime</title>
        <style>
          body { margin: 0; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background: #f7f8f6; color: #18211c; }
          main { padding: 18px; display: grid; grid-template-columns: minmax(240px, 0.85fr) minmax(320px, 1.2fr); gap: 14px; }
          header { grid-column: 1 / -1; display: flex; justify-content: space-between; gap: 12px; align-items: center; }
          h1 { font-size: 20px; margin: 0; }
          h2 { font-size: 15px; margin: 0 0 8px; }
          p { margin: 4px 0 0; color: #5d6a62; }
          button { border: 1px solid #b7c4bb; background: #fff; padding: 8px 10px; border-radius: 6px; cursor: pointer; }
          button.primary { background: #174938; color: white; border-color: #174938; }
          .panel { background: white; border: 1px solid #d8dfd9; border-radius: 8px; padding: 12px; min-height: 120px; }
          .message { width: 100%; text-align: left; display: grid; gap: 4px; margin-bottom: 8px; }
          .message strong { font-size: 13px; }
          .message span, .muted { color: #68756d; font-size: 12px; }
          .pills { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
          .pill { background: #eef3ef; color: #34443b; border-radius: 999px; padding: 3px 8px; font-size: 12px; }
          form { display: grid; gap: 8px; margin-top: 12px; }
          input, select, textarea { width: 100%; box-sizing: border-box; padding: 8px; border: 1px solid #c8d0ca; border-radius: 6px; }
          label { display: grid; gap: 4px; font-size: 12px; color: #5d6a62; }
          .result { color: #174938; font-size: 13px; }
        </style>
      </head>
      <body>
        <main>
          <header>
            <div>
              <h1>Email Runtime</h1>
              <p>Plain app UI running inside the StateFork-managed environment.</p>
            </div>
            <button id="refreshBtn">Refresh</button>
          </header>
          <section class="panel">
            <h2>Mailbox</h2>
            <div id="summary" class="muted">Loading...</div>
            <div id="messages"></div>
          </section>
          <section class="panel">
            <h2>Message Detail</h2>
            <div id="detail" class="muted">Select a message.</div>
          </section>
        </main>
        <script>
          const summaryEl = document.querySelector('#summary');
          const messagesEl = document.querySelector('#messages');
          const detailEl = document.querySelector('#detail');
          const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[char]));
          let selectedId = null;
          async function request(path, options = {}) {
            const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || `Request failed: ${response.status}`);
            return data;
          }
          function pills(values) { return `<div class="pills">${values.map((value) => `<span class="pill">${esc(value)}</span>`).join('')}</div>`; }
          function renderDetail(message) {
            if (!message) { detailEl.textContent = 'Select a message.'; return; }
            detailEl.innerHTML = `
              <strong>${esc(message.subject)}</strong>
              <p>From ${esc(message.from_address)}</p>
              <p>To ${esc(message.to_address)}</p>
              ${pills([message.folder, message.priority, message.is_read ? 'read' : 'unread', ...(message.labels || [])])}
              <p>${esc(message.body)}</p>
              <form id="labelForm"><label>Label<input name="label" placeholder="finance" /></label><button class="primary">Add Label</button></form>
              <form id="moveForm"><label>Folder<select name="folder"><option>Inbox</option><option>Archive</option><option>Spam</option></select></label><button>Move</button></form>
            `;
            document.querySelector('#labelForm').addEventListener('submit', async (event) => {
              event.preventDefault();
              const form = new FormData(event.currentTarget);
              await request(`api/messages/${message.id}/label`, { method: 'POST', body: JSON.stringify({ label: form.get('label'), actor: 'user' }) });
              await refresh();
            });
            document.querySelector('#moveForm').addEventListener('submit', async (event) => {
              event.preventDefault();
              const form = new FormData(event.currentTarget);
              await request(`api/messages/${message.id}/move`, { method: 'POST', body: JSON.stringify({ folder: form.get('folder'), actor: 'user' }) });
              await refresh();
            });
          }
          async function refresh() {
            const mailbox = await request('api/mailbox');
            if (!selectedId && mailbox.messages.length) selectedId = mailbox.messages[0].id;
            summaryEl.textContent = `${mailbox.unread} unread · ${mailbox.drafts} drafts · ${mailbox.messages.length} messages`;
            messagesEl.innerHTML = mailbox.messages.map((message) => `
              <button class="message" data-id="${esc(message.id)}">
                <strong>${esc(message.subject)}</strong>
                <span>${esc(message.from_address)} · ${esc(message.folder)} · ${esc(message.priority)}</span>
              </button>
            `).join('');
            renderDetail(mailbox.messages.find((message) => message.id === selectedId));
          }
          messagesEl.addEventListener('click', async (event) => {
            const button = event.target.closest('button[data-id]');
            if (!button) return;
            selectedId = button.dataset.id;
            await refresh();
          });
          document.querySelector('#refreshBtn').addEventListener('click', refresh);
          refresh().catch((error) => { summaryEl.textContent = error.message; });
        </script>
      </body>
    </html>
    """


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


@app.get("/api/mailbox")
def mailbox() -> dict:
    with db() as conn:
        folders = rows(
            conn.execute(
                """
                SELECT folder, COUNT(*) AS count
                FROM messages
                GROUP BY folder
                ORDER BY folder
                """
            )
        )
        labels = rows(
            conn.execute(
                """
                SELECT label, COUNT(*) AS count
                FROM message_labels
                GROUP BY label
                ORDER BY label
                """
            )
        )
        unread = conn.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE is_read = 0"
        ).fetchone()["count"]
        drafts_count = conn.execute("SELECT COUNT(*) AS count FROM drafts").fetchone()["count"]
        messages = message_rows(conn)
    return {
        "folders": folders,
        "labels": labels,
        "unread": unread,
        "drafts": drafts_count,
        "messages": messages,
    }


@app.get("/api/messages")
def messages() -> dict:
    with db() as conn:
        return {"messages": message_rows(conn)}


@app.get("/api/messages/{message_id}")
def message_detail(message_id: str) -> dict:
    with db() as conn:
        return {"message": message_with_labels(conn, message_id)}


@app.post("/api/messages")
def create_message(payload: CreateMessageRequest) -> dict:
    message_id = (
        strip_required(payload.id, "id")
        if payload.id is not None
        else f"msg-{secrets.token_hex(4)}"
    )
    folder = normalize_folder(payload.folder)
    priority = normalize_priority(payload.priority)
    from_address = strip_required(payload.from_address, "from_address")
    to_address = strip_required(payload.to_address, "to_address")
    subject = strip_required(payload.subject, "subject")
    body = strip_required(payload.body, "body")
    with db() as conn:
        existing = conn.execute("SELECT id FROM messages WHERE id = ?", (message_id,)).fetchone()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"Message already exists: {message_id}")
        conn.execute(
            """
            INSERT INTO messages(
                id, from_address, to_address, subject, body, folder, is_read, priority
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                from_address,
                to_address,
                subject,
                body,
                folder,
                int(payload.is_read),
                priority,
            ),
        )
        audit(
            conn,
            payload.actor,
            "receive",
            f"Received {message_id}: {subject}",
        )
        return {"status": "received", "message": message_with_labels(conn, message_id)}


@app.post("/api/messages/{message_id}/label")
def label_message(message_id: str, payload: LabelMessageRequest) -> dict:
    label = normalize_label(payload.label)
    with db() as conn:
        message_with_labels(conn, message_id)
        cursor = conn.execute(
            "INSERT OR IGNORE INTO message_labels(message_id, label) VALUES (?, ?)",
            (message_id, label),
        )
        if cursor.rowcount:
            conn.execute(
                "UPDATE messages SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (message_id,),
            )
            audit(
                conn,
                payload.actor,
                "label",
                f"Added label {label} to {message_id}",
            )
        return {
            "status": "labeled" if cursor.rowcount else "unchanged",
            "message": message_with_labels(conn, message_id),
        }


@app.post("/api/messages/{message_id}/move")
def move_message(message_id: str, payload: MoveMessageRequest) -> dict:
    folder = normalize_folder(payload.folder)
    with db() as conn:
        message = message_with_labels(conn, message_id)
        if message["folder"] != folder:
            conn.execute(
                """
                UPDATE messages
                SET folder = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (folder, message_id),
            )
            audit(
                conn,
                payload.actor,
                "move",
                f"Moved {message_id} from {message['folder']} to {folder}",
            )
        return {
            "status": "moved" if message["folder"] != folder else "unchanged",
            "message": message_with_labels(conn, message_id),
        }


@app.post("/api/messages/{message_id}/archive")
def archive_message(message_id: str, payload: ActorRequest | None = None) -> dict:
    actor = payload.actor if payload else "user"
    with db() as conn:
        message = message_with_labels(conn, message_id)
        if message["folder"] != "Archive":
            conn.execute(
                """
                UPDATE messages
                SET folder = 'Archive', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (message_id,),
            )
            audit(
                conn,
                actor,
                "archive",
                f"Archived {message_id} from {message['folder']}",
            )
        return {
            "status": "archived" if message["folder"] != "Archive" else "unchanged",
            "message": message_with_labels(conn, message_id),
        }


@app.post("/api/messages/{message_id}/read")
def mark_message_read(message_id: str, payload: ReadMessageRequest) -> dict:
    with db() as conn:
        message = message_with_labels(conn, message_id)
        desired = int(payload.is_read)
        if int(message["is_read"]) != desired:
            conn.execute(
                """
                UPDATE messages
                SET is_read = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (desired, message_id),
            )
            audit(
                conn,
                payload.actor,
                "read" if payload.is_read else "unread",
                f"Marked {message_id} {'read' if payload.is_read else 'unread'}",
            )
        return {
            "status": "read" if payload.is_read else "unread",
            "message": message_with_labels(conn, message_id),
        }


@app.post("/api/drafts")
def create_draft(payload: DraftRequest) -> dict:
    to_address = strip_required(payload.to_address, "to_address")
    subject = strip_required(payload.subject, "subject")
    body = strip_required(payload.body, "body")
    with db() as conn:
        if payload.source_message_id is not None:
            message_with_labels(conn, payload.source_message_id)
        cursor = conn.execute(
            """
            INSERT INTO drafts(source_message_id, to_address, subject, body, status, created_by)
            VALUES (?, ?, ?, ?, 'draft', ?)
            """,
            (
                payload.source_message_id,
                to_address,
                subject,
                body,
                payload.created_by,
            ),
        )
        audit(
            conn,
            payload.created_by,
            "draft",
            f"Created draft {cursor.lastrowid} for {payload.source_message_id or 'new message'}",
        )
        return {"status": "draft", "draft": draft_row(conn, int(cursor.lastrowid))}


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
                "db_path": str(DB_PATH),
            },
            "mailbox": {
                "folders": rows(
                    conn.execute(
                        """
                        SELECT folder, COUNT(*) AS count
                        FROM messages
                        GROUP BY folder
                        ORDER BY folder
                        """
                    )
                ),
                "labels": rows(
                    conn.execute(
                        """
                        SELECT label, COUNT(*) AS count
                        FROM message_labels
                        GROUP BY label
                        ORDER BY label
                        """
                    )
                ),
            },
            "messages": message_rows(conn),
            "drafts": rows(conn.execute("SELECT * FROM drafts ORDER BY id DESC")),
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
    if DB_PATH.exists():
        DB_PATH.unlink()
    init_db()
    return {"status": "reset"}
