from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .ckpt_runtime import CheckpointLiteError, CheckpointLiteRuntime
from .db import copy_db, diff_db, init_db, read_state


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.lower() not in {"0", "false", "no", "off"}


def source_dir() -> Path:
    return Path(os.getenv("AGENT_SAFE_SOURCE_DIR", "/tmp/agent-safe-counter-main")).resolve()


def main_db_path() -> Path:
    return source_dir() / "state.db"


def public_base_url() -> str:
    return os.getenv("AGENT_SAFE_PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def branch_host() -> str:
    return os.getenv("AGENT_SAFE_BRANCH_HOST", "127.0.0.1")


def branch_port_start() -> int:
    return int(os.getenv("AGENT_SAFE_BRANCH_PORT_START", "8400"))


def runtime() -> CheckpointLiteRuntime:
    return CheckpointLiteRuntime(
        binary=os.getenv("AGENT_SAFE_CKPT_BIN", "/users/alexxjk/checkpoint-lite/checkpoint-lite"),
        sessions_dir=os.getenv("AGENT_SAFE_SESSIONS_DIR", "/tmp/checkpoint-sessions-agent-safe-services"),
        session_info_dir=os.getenv("AGENT_SAFE_SESSION_INFO_DIR", "/tmp/checkpoint-sessions-info-agent-safe-services"),
        use_sudo=env_bool("AGENT_SAFE_USE_SUDO", True),
    )


class BranchCreateRequest(BaseModel):
    label: str | None = Field(default=None, max_length=120)


class SnapshotRequest(BaseModel):
    label: str | None = Field(default=None, max_length=120)


@dataclass
class Branch:
    branch_id: str
    label: str | None
    session_id: str
    workdir: Path
    db_path: Path
    port: int
    process: subprocess.Popen[str]
    created_at: float = field(default_factory=time.time)
    snapshots: list[dict[str, Any]] = field(default_factory=list)

    @property
    def direct_url(self) -> str:
        return f"http://{branch_host()}:{self.port}"

    @property
    def proxy_url(self) -> str:
        return f"{public_base_url()}/branches/{self.branch_id}/proxy"

    def to_dict(self) -> dict[str, Any]:
        status = "running" if self.process.poll() is None else "exited"
        return {
            "branch_id": self.branch_id,
            "label": self.label,
            "session_id": self.session_id,
            "workdir": str(self.workdir),
            "db_path": str(self.db_path),
            "port": self.port,
            "direct_url": self.direct_url,
            "proxy_url": self.proxy_url,
            "status": status,
            "snapshots": self.snapshots,
        }


app = FastAPI(title="Agent-Safe Branch Controller", version="0.1.0")
branches: dict[str, Branch] = {}


def ensure_source() -> None:
    source_dir().mkdir(parents=True, exist_ok=True)
    init_db(main_db_path())


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((branch_host(), port)) != 0


def next_port() -> int:
    used = {branch.port for branch in branches.values()}
    port = branch_port_start()
    while port in used or not port_is_free(port):
        port += 1
    return port


def pythonpath_for() -> str:
    src = str(Path(__file__).resolve().parents[2])
    existing = os.environ.get("PYTHONPATH", "")
    return f"{src}:{existing}" if existing else src


def make_workdir_writable(workdir: Path) -> None:
    subprocess.run(["sudo", "-n", "chmod", "0777", str(workdir)], check=False)


def start_branch_service(branch_id: str, db_path: Path, port: int) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["AGENT_SAFE_COUNTER_DB"] = str(db_path)
    env["AGENT_SAFE_BRANCH_ID"] = branch_id
    env["PYTHONPATH"] = pythonpath_for()
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agent_safe_services.counter_app:app", "--host", branch_host(), "--port", str(port)],
        cwd=str(db_path.parent),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def stop_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def wait_ready(branch: Branch, timeout: float = 12.0) -> None:
    deadline = time.time() + timeout
    url = f"{branch.direct_url}/health"
    while time.time() < deadline:
        if branch.process.poll() is not None:
            _, err = branch.process.communicate(timeout=1)
            raise HTTPException(status_code=500, detail=f"branch service exited early: {err[-1000:]}")
        try:
            with httpx.Client(timeout=1.0) as client:
                if client.get(url).status_code == 200:
                    return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise HTTPException(status_code=500, detail=f"timed out waiting for {url}")


@app.on_event("startup")
def startup() -> None:
    ensure_source()


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "source_dir": str(source_dir()), "main_db": str(main_db_path()), "public_base_url": public_base_url(), "branch_count": len(branches)}


@app.get("/main/value")
def main_value() -> dict[str, Any]:
    ensure_source()
    return read_state(main_db_path())


@app.get("/branches")
def list_branches() -> dict[str, Any]:
    return {"branches": [branch.to_dict() for branch in branches.values()]}


@app.post("/branches")
def create_branch(payload: BranchCreateRequest | None = None) -> dict[str, Any]:
    ensure_source()
    branch_id = f"br-{uuid.uuid4().hex[:8]}"
    rt = runtime()
    try:
        init_result = rt.init(source_dir())
        base_snapshot = f"{branch_id}-base"
        rt.create(init_result.session_id, base_snapshot, pid=-1)
        make_workdir_writable(init_result.workdir)
    except CheckpointLiteError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    port = next_port()
    db_path = init_result.workdir / "state.db"
    proc = start_branch_service(branch_id, db_path, port)
    branch = Branch(
        branch_id=branch_id,
        label=payload.label if payload else None,
        session_id=init_result.session_id,
        workdir=init_result.workdir,
        db_path=db_path,
        port=port,
        process=proc,
        snapshots=[{"snapshot_id": base_snapshot, "label": "base", "created_at": time.time()}],
    )
    branches[branch_id] = branch
    try:
        wait_ready(branch)
    except Exception:
        stop_process(proc)
        rt.cleanup(init_result.session_id)
        branches.pop(branch_id, None)
        raise
    return branch.to_dict()


def require_branch(branch_id: str) -> Branch:
    branch = branches.get(branch_id)
    if branch is None:
        raise HTTPException(status_code=404, detail=f"unknown branch: {branch_id}")
    return branch


@app.post("/branches/{branch_id}/snapshots")
def snapshot_branch(branch_id: str, payload: SnapshotRequest | None = None) -> dict[str, Any]:
    branch = require_branch(branch_id)
    snapshot_id = f"snap-{uuid.uuid4().hex[:8]}"
    stop_process(branch.process)
    try:
        runtime().create(branch.session_id, snapshot_id, pid=-1)
        make_workdir_writable(branch.workdir)
    except CheckpointLiteError as exc:
        branch.process = start_branch_service(branch.branch_id, branch.db_path, branch.port)
        wait_ready(branch)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    branch.process = start_branch_service(branch.branch_id, branch.db_path, branch.port)
    wait_ready(branch)
    snapshot = {"snapshot_id": snapshot_id, "label": payload.label if payload else None, "created_at": time.time()}
    branch.snapshots.append(snapshot)
    return snapshot


@app.post("/branches/{branch_id}/restore/{snapshot_id}")
def restore_branch(branch_id: str, snapshot_id: str) -> dict[str, Any]:
    branch = require_branch(branch_id)
    if snapshot_id not in {snap["snapshot_id"] for snap in branch.snapshots}:
        raise HTTPException(status_code=404, detail=f"unknown snapshot for branch: {snapshot_id}")
    stop_process(branch.process)
    try:
        runtime().restore(branch.session_id, snapshot_id)
        make_workdir_writable(branch.workdir)
    except CheckpointLiteError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    branch.process = start_branch_service(branch.branch_id, branch.db_path, branch.port)
    wait_ready(branch)
    return branch.to_dict()


@app.get("/branches/{branch_id}/diff")
def branch_diff(branch_id: str) -> dict[str, Any]:
    branch = require_branch(branch_id)
    ensure_source()
    return diff_db(main_db_path(), branch.db_path)


@app.post("/branches/{branch_id}/commit")
def commit_branch(branch_id: str) -> dict[str, Any]:
    branch = require_branch(branch_id)
    ensure_source()
    before = diff_db(main_db_path(), branch.db_path)
    copy_db(branch.db_path, main_db_path())
    return {"status": "committed", "branch_id": branch_id, "diff_before_commit": before}


@app.delete("/branches/{branch_id}")
def delete_branch(branch_id: str) -> dict[str, Any]:
    branch = require_branch(branch_id)
    stop_process(branch.process)
    runtime().cleanup(branch.session_id)
    branches.pop(branch_id, None)
    return {"status": "deleted", "branch_id": branch_id}


@app.api_route("/branches/{branch_id}/proxy/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def branch_proxy(branch_id: str, path: str, request: Request) -> Response:
    branch = require_branch(branch_id)
    target = f"{branch.direct_url}/{path}"
    body = await request.body()
    headers = {key: value for key, value in request.headers.items() if key.lower() != "host"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        proxied = await client.request(request.method, target, content=body, headers=headers, params=request.query_params)
    return Response(content=proxied.content, status_code=proxied.status_code, media_type=proxied.headers.get("content-type"))


@app.on_event("shutdown")
def shutdown() -> None:
    for branch in list(branches.values()):
        stop_process(branch.process)
