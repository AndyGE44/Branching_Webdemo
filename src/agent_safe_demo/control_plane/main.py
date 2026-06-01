from __future__ import annotations

import base64
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
import secrets
from typing import AsyncIterator
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent_safe_demo.control_plane.app_registry import AppSpec, get_app_spec, list_app_specs
from agent_safe_demo.control_plane.branching import (
    BranchError,
    DirtyBranchError,
    StateForkBackend,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DEMO_AUTH_USER = os.getenv("DEMO_AUTH_USER", "demo")
DEMO_AUTH_PASSWORD = os.getenv("DEMO_AUTH_PASSWORD")
DEMO_AUTH_REALM = os.getenv("DEMO_AUTH_REALM", "Agent-Safe Demo")
WORKSPACE_BASE_ID: str | None = None
WORKSPACE_BRANCH_ID: str | None = None
WORKSPACE_INITIAL_SNAPSHOT_LABEL = "Initial checkpoint"
CURRENT_APP: AppSpec = get_app_spec()


def statefork_kwargs_from_env() -> dict:
    kwargs = json.loads(os.getenv("DEMO_STATEFORK_KWARGS", "{}"))
    if "build" not in kwargs and "DEMO_STATEFORK_BUILD" in os.environ:
        kwargs["build"] = os.getenv("DEMO_STATEFORK_BUILD", "1") != "0"
    return kwargs


def create_branch_backend(app_spec: AppSpec | None = None) -> StateForkBackend:
    selected_app = app_spec or CURRENT_APP
    return StateForkBackend(
        selected_app.project_root,
        selected_app.db_path,
        statefork_root=Path(
            os.getenv("DEMO_STATEFORK_ROOT", selected_app.project_root.parent / "StateFork")
        ),
        statefork_method=os.getenv("DEMO_STATEFORK_METHOD", "ckpt_build"),
        statefork_cwd=Path(os.getenv("DEMO_STATEFORK_CWD", str(selected_app.project_root))),
        statefork_kwargs=statefork_kwargs_from_env(),
        host=os.getenv("DEMO_BRANCH_HOST", "127.0.0.1"),
        port_start=int(os.getenv("DEMO_BRANCH_PORT_START", "8300")),
        app_id=selected_app.id,
        app_label=selected_app.label,
        app_uvicorn_target_value=selected_app.uvicorn_target,
        app_db_env_var=selected_app.db_env_var,
        health_path=selected_app.health_path,
        agent_demo_actions=list(selected_app.agent_demo_actions or []),
    )


branch_backend = create_branch_backend(CURRENT_APP)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    CURRENT_APP.init_db()
    yield


app = FastAPI(
    title="Agent-Safe Multi-App Workspace Controller",
    description="A StateFork web shell that manages plain app-plane services.",
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


class BaseCheckpointRequest(BaseModel):
    label: str | None = Field(default=None, max_length=80)


class BranchSnapshotRequest(BaseModel):
    label: str | None = Field(default=None, max_length=80)


class BranchRestoreRequest(BaseModel):
    snapshot_id: str
    force: bool = False


class WorkspaceSnapshotRequest(BaseModel):
    label: str | None = Field(default=None, max_length=80)


class WorkspaceRestoreRequest(BaseModel):
    snapshot_id: str
    force: bool = False


def demo_auth_enabled() -> bool:
    return bool(DEMO_AUTH_PASSWORD)


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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def branch_error(error: BranchError) -> JSONResponse:
    status_code = 409 if isinstance(error, DirtyBranchError) else 400
    return JSONResponse({"detail": str(error)}, status_code=status_code)


def reset_workspace_handles() -> None:
    global WORKSPACE_BASE_ID, WORKSPACE_BRANCH_ID
    WORKSPACE_BASE_ID = None
    WORKSPACE_BRANCH_ID = None


def app_payload() -> dict:
    return CURRENT_APP.public_dict()


def running_workspace_branch() -> dict | None:
    global WORKSPACE_BRANCH_ID
    branches = branch_backend.list_branches()
    if WORKSPACE_BRANCH_ID:
        for branch in branches:
            if branch["id"] == WORKSPACE_BRANCH_ID and branch["status"] == "running":
                return branch
        WORKSPACE_BRANCH_ID = None
    for branch in branches:
        if branch["status"] == "running":
            WORKSPACE_BRANCH_ID = branch["id"]
            return branch
    return None


def workspace_payload(branch: dict) -> dict:
    runtime_ui_path = CURRENT_APP.runtime_ui_path
    return {
        "app": app_payload(),
        "workspace": {
            "mode": "runtime-checkpoints",
            "app_id": CURRENT_APP.id,
            "base_id": branch.get("base_id") or WORKSPACE_BASE_ID,
            "branch_id": branch["id"],
            "runtime_url": branch["url"],
            "runtime_proxy_url": "/runtime",
            "runtime_ui_url": f"/runtime{runtime_ui_path}",
            "state_path": CURRENT_APP.state_path,
            "current_snapshot_id": branch.get("current_snapshot_id"),
            "dirty": branch.get("dirty", False),
        },
        "branch": branch,
        "backend": branch_backend.status(),
    }


def ensure_workspace() -> dict:
    global WORKSPACE_BASE_ID, WORKSPACE_BRANCH_ID
    CURRENT_APP.init_db()
    branch = running_workspace_branch()
    if branch is None:
        base = branch_backend.create_base(label=f"{CURRENT_APP.label} workspace start")
        WORKSPACE_BASE_ID = base["id"]
        branch = branch_backend.create_branch(base_id=base["id"])
        WORKSPACE_BRANCH_ID = branch["id"]
        branch = branch_backend.save_snapshot(
            branch["id"],
            label=WORKSPACE_INITIAL_SNAPSHOT_LABEL,
        )["branch"]
    else:
        WORKSPACE_BASE_ID = branch.get("base_id") or WORKSPACE_BASE_ID
    return workspace_payload(branch)


def workspace_branch_id() -> str:
    return ensure_workspace()["branch"]["id"]


def switch_current_app(app_id: str) -> dict:
    global CURRENT_APP, branch_backend
    next_app = get_app_spec(app_id)
    if next_app.id == CURRENT_APP.id:
        return {"cleanup": {"branches_deleted": 0, "bases_deleted": 0}, **app_selection_payload()}
    cleanup = branch_backend.reset()
    reset_workspace_handles()
    CURRENT_APP = next_app
    CURRENT_APP.init_db()
    branch_backend = create_branch_backend(CURRENT_APP)
    return {"cleanup": cleanup, **app_selection_payload()}


def app_selection_payload() -> dict:
    return {
        "current_app_id": CURRENT_APP.id,
        "apps": [spec.public_dict() for spec in list_app_specs()],
    }


@app.get("/api/apps")
def apps() -> dict:
    return app_selection_payload()


@app.post("/api/apps/{app_id}/select")
def select_app(app_id: str) -> dict:
    try:
        return switch_current_app(app_id)
    except ValueError as error:
        return JSONResponse({"detail": str(error)}, status_code=404)
    except BranchError as error:
        return branch_error(error)


def runtime_target_url(branch: dict, path: str, query_string: bytes = b"") -> str:
    suffix = f"/{path}" if path else "/"
    query = query_string.decode("utf-8")
    return f"{branch['url']}{suffix}{'?' + query if query else ''}"


def proxy_headers(request: Request) -> dict[str, str]:
    blocked = {"host", "content-length", "accept-encoding", "connection"}
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in blocked
    }


def proxied_response(content: bytes, status_code: int, content_type: str | None) -> Response:
    headers = {"content-type": content_type} if content_type else None
    return Response(content=content, status_code=status_code, headers=headers)


@app.api_route("/runtime", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@app.api_route("/runtime/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def runtime_proxy(request: Request, path: str = "") -> Response:
    try:
        branch = ensure_workspace()["branch"]
    except BranchError as error:
        return branch_error(error)

    body = await request.body()
    target = runtime_target_url(branch, path, request.scope.get("query_string", b""))
    proxy_request = urlrequest.Request(
        target,
        data=body or None,
        headers=proxy_headers(request),
        method=request.method,
    )
    try:
        with urlrequest.urlopen(proxy_request, timeout=15) as proxy_response:
            content = proxy_response.read()
            content_type = proxy_response.headers.get("content-type")
            return proxied_response(content, proxy_response.status, content_type)
    except HTTPError as error:
        content = error.read()
        content_type = error.headers.get("content-type")
        return proxied_response(content, error.code, content_type)
    except URLError as error:
        return JSONResponse({"detail": f"Runtime proxy failed: {error}"}, status_code=502)


@app.get("/api/backend")
def backend_status() -> dict:
    return branch_backend.status()


@app.post("/api/reset")
def reset() -> dict:
    cleanup = branch_backend.reset()
    reset_workspace_handles()
    if CURRENT_APP.db_path.exists():
        CURRENT_APP.db_path.unlink()
    CURRENT_APP.init_db()
    return {"status": "reset", "cleanup": cleanup, "app": app_payload()}


@app.get("/api/workspace")
def get_workspace() -> dict:
    try:
        return ensure_workspace()
    except BranchError as error:
        return branch_error(error)


@app.get("/api/workspace/dirty")
def workspace_dirty() -> dict:
    try:
        return branch_backend.dirty(workspace_branch_id())
    except BranchError as error:
        return branch_error(error)


@app.post("/api/workspace/run-agent")
def run_workspace_agent() -> dict:
    try:
        result = branch_backend.run_agent_demo(workspace_branch_id())
        return {**result, "workspace": workspace_payload(result["branch"])["workspace"]}
    except BranchError as error:
        return branch_error(error)


@app.post("/api/workspace/snapshots")
def save_workspace_snapshot(
    payload: WorkspaceSnapshotRequest | None = None,
) -> dict:
    try:
        label = payload.label if payload else None
        result = branch_backend.save_snapshot(workspace_branch_id(), label=label)
        return {**result, "workspace": workspace_payload(result["branch"])["workspace"]}
    except BranchError as error:
        return branch_error(error)


@app.post("/api/workspace/restore")
def restore_workspace_snapshot(payload: WorkspaceRestoreRequest) -> dict:
    try:
        result = branch_backend.restore_snapshot(
            workspace_branch_id(),
            snapshot_id=payload.snapshot_id,
            force=payload.force,
        )
        return {**result, "workspace": workspace_payload(result["branch"])["workspace"]}
    except BranchError as error:
        return branch_error(error)


@app.post("/api/workspace/reset")
def reset_workspace() -> dict:
    cleanup = branch_backend.reset()
    reset_workspace_handles()
    if CURRENT_APP.db_path.exists():
        CURRENT_APP.db_path.unlink()
    CURRENT_APP.init_db()
    try:
        workspace = ensure_workspace()
    except BranchError as error:
        return branch_error(error)
    return {"status": "reset", "cleanup": cleanup, **workspace}


@app.get("/api/bases")
def list_bases() -> dict:
    return {"backend": branch_backend.name, "bases": branch_backend.list_bases()}


@app.post("/api/bases")
def create_base(payload: BaseCheckpointRequest | None = None) -> dict:
    try:
        label = payload.label if payload else None
        return {"base": branch_backend.create_base(label=label)}
    except BranchError as error:
        return branch_error(error)


@app.post("/api/bases/{base_id}/branches")
def create_branch_from_base(base_id: str) -> dict:
    try:
        return {"branch": branch_backend.create_branch(base_id=base_id)}
    except BranchError as error:
        return branch_error(error)


@app.delete("/api/bases/{base_id}")
def delete_base(base_id: str) -> dict:
    try:
        return branch_backend.delete_base(base_id)
    except BranchError as error:
        return branch_error(error)


@app.get("/api/branches")
def list_branches() -> dict:
    return {"backend": branch_backend.name, "branches": branch_backend.list_branches()}


@app.post("/api/branches")
def create_branch() -> dict:
    try:
        return {"branch": branch_backend.create_branch()}
    except BranchError as error:
        return branch_error(error)


@app.post("/api/branches/{branch_id}/run-agent-demo")
def run_branch_agent_demo(branch_id: str) -> dict:
    try:
        return branch_backend.run_agent_demo(branch_id)
    except BranchError as error:
        return branch_error(error)


@app.get("/api/branches/{branch_id}/dirty")
def branch_dirty(branch_id: str) -> dict:
    try:
        return branch_backend.dirty(branch_id)
    except BranchError as error:
        return branch_error(error)


@app.post("/api/branches/{branch_id}/snapshots")
def save_branch_snapshot(
    branch_id: str,
    payload: BranchSnapshotRequest | None = None,
) -> dict:
    try:
        label = payload.label if payload else None
        return branch_backend.save_snapshot(branch_id, label=label)
    except BranchError as error:
        return branch_error(error)


@app.post("/api/branches/{branch_id}/restore")
def restore_branch_snapshot(
    branch_id: str,
    payload: BranchRestoreRequest,
) -> dict:
    try:
        return branch_backend.restore_snapshot(
            branch_id,
            snapshot_id=payload.snapshot_id,
            force=payload.force,
        )
    except BranchError as error:
        return branch_error(error)


@app.get("/api/branches/{branch_id}/diff")
def branch_diff(branch_id: str) -> dict:
    try:
        return branch_backend.diff(branch_id)
    except BranchError as error:
        return branch_error(error)


@app.post("/api/branches/{branch_id}/commit")
def commit_branch(branch_id: str) -> dict:
    try:
        return branch_backend.commit(branch_id)
    except BranchError as error:
        return branch_error(error)


@app.post("/api/branches/{branch_id}/discard")
def discard_branch(branch_id: str) -> dict:
    try:
        return branch_backend.discard(branch_id)
    except BranchError as error:
        return branch_error(error)
