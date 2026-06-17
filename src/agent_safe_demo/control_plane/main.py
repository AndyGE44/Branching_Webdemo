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

from agent_safe_demo.control_plane.app_registry import (
    AppSpec,
    get_app_spec,
    list_app_specs,
    list_manifest_errors,
)
from agent_safe_demo.control_plane.branching import (
    BranchError,
    DirtyBranchError,
    StateForkBackend,
)
from agent_safe_demo.control_plane.commit_store import CommitStore

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DEMO_AUTH_USER = os.getenv("DEMO_AUTH_USER", "demo")
DEMO_AUTH_PASSWORD = os.getenv("DEMO_AUTH_PASSWORD")
DEMO_AUTH_REALM = os.getenv("DEMO_AUTH_REALM", "Agent-Safe Demo")
WORKSPACE_BASE_ID: str | None = None
WORKSPACE_BRANCH_ID: str | None = None
WORKSPACE_INITIAL_SNAPSHOT_LABEL = "Initial checkpoint"
CURRENT_APP: AppSpec = get_app_spec()


def control_plane_db_path() -> Path:
    return Path(
        os.getenv(
            "DEMO_CONTROL_PLANE_DB_PATH",
            str(CURRENT_APP.project_root / "control_plane_metadata.db"),
        )
    )


commit_store = CommitStore(control_plane_db_path())


def statefork_kwargs_from_env() -> dict:
    kwargs = json.loads(os.getenv("DEMO_STATEFORK_KWARGS", "{}"))
    if "build" not in kwargs and "DEMO_STATEFORK_BUILD" in os.environ:
        kwargs["build"] = os.getenv("DEMO_STATEFORK_BUILD", "1") != "0"
    return kwargs


class DataBackendConfig:
    """Resolved data-tier configuration for an app."""

    def __init__(
        self,
        backend: str,
        dolt_dir: Path | None = None,
        runtime_env: dict[str, str] | None = None,
        server_params: dict | None = None,
    ) -> None:
        self.backend = backend
        self.dolt_dir = dolt_dir
        self.runtime_env = runtime_env or {}
        self.server_params = server_params


def data_backend_config(app_spec: AppSpec) -> DataBackendConfig:
    """Resolve the data-tier backend for an app from the environment.

    For the Dolt backends (architecture A) the same external repo is shared by
    the control plane's data tier and the in-runtime app, so the runtime env is
    augmented to point the app at it. The repo MUST live outside the per-branch
    checkpoint workdir. ``dolt_server`` adds a long-lived ``dolt sql-server``
    (managed in lifespan) and connection parameters.
    """
    # db_env_var looks like "DEMO_INVENTORY_DB_PATH"; derive the sibling vars.
    db_env_var = app_spec.db_env_var
    prefix = db_env_var[: -len("_DB_PATH")] if db_env_var.endswith("_DB_PATH") else db_env_var
    backend = os.getenv(
        f"{prefix}_DB_BACKEND", os.getenv("DEMO_DATA_BACKEND", "sqlite")
    ).lower()

    if backend not in ("dolt", "dolt_server"):
        return DataBackendConfig("sqlite")

    # Default matches the app store's own default (DB_PATH stem + "_dolt").
    default_dir = app_spec.db_path.with_name(app_spec.db_path.stem + "_dolt")
    dolt_dir = Path(os.getenv(f"{prefix}_DOLT_DIR", str(default_dir))).resolve()

    if backend == "dolt":
        runtime_env = {
            f"{prefix}_DB_BACKEND": "dolt",
            f"{prefix}_DOLT_DIR": str(dolt_dir),
        }
        return DataBackendConfig("dolt", dolt_dir=dolt_dir, runtime_env=runtime_env)

    # dolt_server: a persistent MySQL-protocol server over the same repo.
    host = os.getenv(f"{prefix}_DOLT_HOST", "127.0.0.1")
    port = int(os.getenv(f"{prefix}_DOLT_PORT", "3306"))
    database = os.getenv(f"{prefix}_DOLT_DB", dolt_dir.name)  # dolt db name = repo dir name
    server_params = {"host": host, "port": port, "database": database}
    runtime_env = {
        f"{prefix}_DB_BACKEND": "dolt_server",
        f"{prefix}_DOLT_HOST": host,
        f"{prefix}_DOLT_PORT": str(port),
        f"{prefix}_DOLT_DB": database,
    }
    return DataBackendConfig(
        "dolt_server", dolt_dir=dolt_dir, runtime_env=runtime_env, server_params=server_params
    )


def create_branch_backend(app_spec: AppSpec | None = None) -> StateForkBackend:
    selected_app = app_spec or CURRENT_APP
    default_statefork_root = selected_app.project_root.parent / "Andy_StateFork"
    statefork_root = Path(os.getenv("DEMO_STATEFORK_ROOT", default_statefork_root))
    cfg = data_backend_config(selected_app)
    state_env = {**dict(selected_app.state_env), **cfg.runtime_env}
    return StateForkBackend(
        selected_app.project_root,
        selected_app.db_path,
        statefork_root=statefork_root,
        statefork_method=os.getenv("DEMO_STATEFORK_METHOD", "ckpt_build"),
        statefork_cwd=Path(os.getenv("DEMO_STATEFORK_CWD", str(statefork_root))),
        statefork_kwargs=statefork_kwargs_from_env(),
        host=os.getenv("DEMO_BRANCH_HOST", "127.0.0.1"),
        port_start=int(os.getenv("DEMO_BRANCH_PORT_START", "8300")),
        app_id=selected_app.id,
        app_label=selected_app.label,
        app_uvicorn_target_value=selected_app.uvicorn_target,
        app_db_env_var=selected_app.db_env_var,
        health_path=selected_app.health_path,
        runtime_command=selected_app.runtime_command,
        runtime_cwd=selected_app.runtime_cwd,
        runtime_port_env=selected_app.runtime_port_env,
        runtime_type=selected_app.runtime_type,
        build_dockerfile_dir=selected_app.build_dockerfile_dir,
        state_files=list(selected_app.state_files),
        state_env=state_env,
        manifest_path=selected_app.manifest_path,
        agent_demo_actions=list(selected_app.agent_demo_actions or []),
        data_backend=cfg.backend,
        dolt_dir=cfg.dolt_dir,
        dolt_bin=os.getenv("DEMO_DOLT_BIN", "dolt"),
        server_params=cfg.server_params,
    )


branch_backend = create_branch_backend(CURRENT_APP)


dolt_sql_server = None  # set when the current app uses the dolt_server backend


def start_dolt_server_if_needed() -> None:
    """Start a long-lived dolt sql-server for the current app's data tier.

    Must run before CURRENT_APP.init_db() so the app seeds via the server. The
    server connection env is exported so the in-process app store connects to
    the same server the control plane manages.
    """
    global dolt_sql_server
    cfg = data_backend_config(CURRENT_APP)
    if cfg.backend != "dolt_server" or cfg.server_params is None:
        return
    from agent_safe_demo.control_plane.dolt_server import DoltSqlServer

    server = DoltSqlServer(
        cfg.dolt_dir,
        host=cfg.server_params["host"],
        port=cfg.server_params["port"],
        dolt_bin=os.getenv("DEMO_DOLT_BIN", "dolt"),
    )
    server.start()
    dolt_sql_server = server
    # Export connection env so CURRENT_APP.init_db()'s store reaches the server.
    for key, value in cfg.runtime_env.items():
        os.environ[key] = value


def stop_dolt_server_if_needed() -> None:
    global dolt_sql_server
    if dolt_sql_server is not None:
        dolt_sql_server.stop()
        dolt_sql_server = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    start_dolt_server_if_needed()
    CURRENT_APP.init_db()
    commit_store.init_db()
    try:
        yield
    finally:
        stop_dolt_server_if_needed()


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


class WorkspaceCommitRequest(BaseModel):
    label: str | None = Field(default=None, max_length=80)
    message: str | None = Field(default=None, max_length=500)
    author: str = Field(default="user", max_length=80)


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


def app_head_payload() -> dict | None:
    head = commit_store.app_head(CURRENT_APP.id)
    if not head:
        return None
    active_base_id = getattr(branch_backend, "head_base_id", None)
    return {**head, "active": head["base_id"] == active_base_id}


def commit_history_payload(limit: int = 5) -> list[dict]:
    return commit_store.list_commits(CURRENT_APP.id, limit=limit)


def workspace_payload(branch: dict) -> dict:
    runtime_ui_path = CURRENT_APP.runtime_ui_path
    head_commit = app_head_payload()
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
            "head_commit_id": head_commit["id"] if head_commit and head_commit["active"] else None,
            "runtime_command": CURRENT_APP.runtime_command,
            "state_files": branch_backend.state_file_fingerprints(
                Path(branch["work_dir"]) if branch.get("work_dir") else None
            ),
        },
        "branch": branch,
        "backend": branch_backend.status(),
        "app_head": head_commit,
        "commits": commit_history_payload(),
    }


def ensure_workspace() -> dict:
    global WORKSPACE_BASE_ID, WORKSPACE_BRANCH_ID
    CURRENT_APP.init_db()
    branch = running_workspace_branch()
    if branch is None:
        head_base_id = getattr(branch_backend, "head_base_id", None)
        if head_base_id:
            branch = branch_backend.create_branch(base_id=head_base_id)
        else:
            base = branch_backend.create_base(label=f"{CURRENT_APP.label} workspace start")
            branch = branch_backend.create_branch(base_id=base["id"])
        WORKSPACE_BASE_ID = branch.get("base_id")
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
        "manifest_errors": list_manifest_errors(),
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

    if branch.get("checkpointing"):
        return JSONResponse({"detail": "Runtime is checkpointing; retry shortly."}, status_code=503)

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


@app.get("/api/workspace/commits")
def list_workspace_commits() -> dict:
    return {
        "app": app_payload(),
        "head": app_head_payload(),
        "commits": commit_history_payload(limit=20),
    }


@app.post("/api/workspace/commit")
def commit_workspace(payload: WorkspaceCommitRequest | None = None) -> dict:
    try:
        current_workspace = ensure_workspace()
        branch_id = current_workspace["branch"]["id"]
        parent_head = app_head_payload()
        parent_commit_id = (
            parent_head["id"] if parent_head and parent_head.get("active") else None
        )
        diff = branch_backend.diff(branch_id)
        label = (payload.label if payload else None) or f"{CURRENT_APP.label} update"
        label = label.strip() or f"{CURRENT_APP.label} update"
        message = ((payload.message if payload else None) or "").strip()
        author = ((payload.author if payload else None) or "user").strip() or "user"

        result = branch_backend.commit(branch_id)
        head_base = result["head_base"]
        commit = commit_store.create_commit(
            app_id=CURRENT_APP.id,
            parent_commit_id=parent_commit_id,
            base_id=head_base["id"],
            branch_id=branch_id,
            checkpoint_id=head_base["checkpoint_id"],
            label=label,
            message=message,
            author=author,
            diff=diff,
        )
        reset_workspace_handles()
        next_workspace = ensure_workspace()
        return {
            "status": "committed",
            "commit": commit,
            "committed_branch": result["branch"],
            "head_base": head_base,
            **next_workspace,
        }
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
