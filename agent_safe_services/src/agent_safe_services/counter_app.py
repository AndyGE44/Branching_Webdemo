from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .db import apply_delta, init_db, read_state, reset_db


def db_path() -> Path:
    return Path(os.getenv("AGENT_SAFE_COUNTER_DB", "./state.db")).resolve()


class DeltaRequest(BaseModel):
    amount: int = Field(default=1, ge=1, le=1_000_000)
    actor: str = Field(default="user", min_length=1, max_length=80)


app = FastAPI(title="Agent-Safe Counter Service", version="0.1.0")


@app.on_event("startup")
def startup() -> None:
    init_db(db_path())


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "db_path": str(db_path())}


@app.get("/value")
def value() -> dict[str, Any]:
    return read_state(db_path())


@app.post("/increment")
def increment(payload: DeltaRequest) -> dict[str, Any]:
    return apply_delta(db_path(), payload.amount, payload.actor, "increment")


@app.post("/decrement")
def decrement(payload: DeltaRequest) -> dict[str, Any]:
    return apply_delta(db_path(), -payload.amount, payload.actor, "decrement")


@app.post("/reset")
def reset() -> dict[str, Any]:
    return reset_db(db_path())
