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


def create_branch_backend(app_spec: AppSpec | None = None) -> StateForkBackend:
    selected_app = app_spec or CURRENT_APP
    default_statefork_root = selected_app.project_root.parent / "Andy_StateFork"
    statefork_root = Path(os.getenv("DEMO_STATEFORK_ROOT", default_statefork_root))
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
        state_env=dict(selected_app.state_env),
        manifest_path=selected_app.manifest_path,
        agent_demo_actions=list(selected_app.agent_demo_actions or []),
        db_backed=selected_app.db_backed,
    )


branch_backend = create_branch_backend(CURRENT_APP)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    CURRENT_APP.init_db()
    commit_store.init_db()
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


# Cookies the embedded shopgym storefronts set on the control-plane origin to
# hold the in-memory cart id (and Hydrogen session). They persist in the browser
# across shop switches and resets, and because the mock-api assigns deterministic
# cart ids the same id is valid in the next shop — so the cart appears to carry
# over. Clearing them when the workspace context changes gives each shop/reset a
# fresh cart. Harmless for the db-backed apps (they never set these).
STOREFRONT_COOKIES = ("cart", "session")


def clear_storefront_cookies(response: Response) -> None:
    for name in STOREFRONT_COOKIES:
        response.delete_cookie(name, path="/")


@app.post("/api/apps/{app_id}/select")
def select_app(app_id: str, response: Response) -> dict:
    try:
        result = switch_current_app(app_id)
        clear_storefront_cookies(response)
        return result
    except ValueError as error:
        return JSONResponse({"detail": str(error)}, status_code=404)
    except BranchError as error:
        return branch_error(error)


RUNTIME_MOUNT = "/runtime"
# Paths a website runtime (the shopgym Hydrogen storefronts) serves at its ORIGIN
# ROOT rather than under the basename: Vite assets, product images, health, and
# other dist/client static files. Everything else root-relative is a document
# route and must be re-prefixed with the mount so the basename-aware server matches.
RUNTIME_ROOT_PREFIXES = (
    "/assets/",
    "/images/",
    "/build/",
    "/@",
    "/favicon",
    "/health",
    "/robots.txt",
    "/sitemap",
)


def runtime_forward_path(request_path: str) -> str:
    """Map an incoming control-plane path to the path to request on a website
    runtime. The storefront server runs with basename=/runtime (see run-shop.sh
    and the shop Dockerfile), so /runtime/* documents and root static assets pass
    through unchanged, while a stray root-relative document link (e.g. a hardcoded
    <a href="/pages/about">) gets the mount prepended so the server matches it."""
    if request_path == RUNTIME_MOUNT or request_path.startswith(RUNTIME_MOUNT + "/"):
        return request_path
    if request_path.startswith(RUNTIME_ROOT_PREFIXES):
        return request_path
    return RUNTIME_MOUNT + request_path


def proxy_headers(request: Request) -> dict[str, str]:
    blocked = {"host", "content-length", "accept-encoding", "connection"}
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in blocked
    }


def proxied_response(content: bytes, status_code: int, upstream_headers) -> Response:
    media_type = upstream_headers.get("content-type") if upstream_headers is not None else None
    response = Response(content=content, status_code=status_code, media_type=media_type)
    if upstream_headers is not None:
        # Forward Set-Cookie (the Hydrogen session that holds the cart id — may be
        # several) and other response headers the embedded app relies on. Hop-by-hop
        # and length headers are managed by Starlette, so they are intentionally skipped.
        for cookie in upstream_headers.get_all("set-cookie", []):
            response.raw_headers.append((b"set-cookie", cookie.encode("latin-1")))
        for key in ("location", "cache-control"):
            value = upstream_headers.get(key)
            if value:
                response.headers[key] = value
    return response


def runtime_proxy_headers(request: Request, branch: dict) -> dict[str, str]:
    headers = {
        key: value
        for key, value in proxy_headers(request).items()
        if key.lower() not in {"origin", "referer"}
    }
    # We proxy to branch.url, so urllib sends Host=<branch host>. React Router's
    # single-fetch action guard rejects a POST whose Origin host != Host ("host
    # header does not match origin header"), which breaks add-to-cart through the
    # control-plane origin. Align Origin to the runtime so the guard passes.
    headers["Origin"] = branch["url"]
    return headers


async def proxy_request_to_branch(request: Request, branch: dict, forward_path: str) -> Response:
    if branch.get("checkpointing"):
        return JSONResponse({"detail": "Runtime is checkpointing; retry shortly."}, status_code=503)

    body = await request.body()
    query = request.scope.get("query_string", b"").decode("utf-8")
    target = f"{branch['url']}{forward_path}{'?' + query if query else ''}"
    proxy_request = urlrequest.Request(
        target,
        data=body or None,
        headers=runtime_proxy_headers(request, branch),
        method=request.method,
    )
    try:
        with urlrequest.urlopen(proxy_request, timeout=30) as proxy_response:
            content = proxy_response.read()
            return proxied_response(content, proxy_response.status, proxy_response.headers)
    except HTTPError as error:
        content = error.read()
        return proxied_response(content, error.code, error.headers)
    except URLError as error:
        return JSONResponse({"detail": f"Runtime proxy failed: {error}"}, status_code=502)


def branch_forward_path(request: Request, path: str) -> str:
    # Website apps run with basename=/runtime and keep the full path (assets at
    # root, documents under /runtime). db-backed apps (email/inventory) serve at
    # root with relative URLs, so strip the mount as before.
    if CURRENT_APP.db_backed:
        return f"/{path}"
    return runtime_forward_path(request.url.path)


@app.api_route("/runtime", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
@app.api_route("/runtime/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def runtime_proxy(request: Request, path: str = "") -> Response:
    try:
        branch = ensure_workspace()["branch"]
    except BranchError as error:
        return branch_error(error)
    return await proxy_request_to_branch(request, branch, branch_forward_path(request, path))


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
def reset_workspace(response: Response) -> dict:
    cleanup = branch_backend.reset()
    reset_workspace_handles()
    # A reset is a full wipe: drop committed heads/history so we do not return to
    # a previously committed state, and clear the storefront cart cookie.
    commit_store.reset_app(CURRENT_APP.id)
    if CURRENT_APP.db_path.exists():
        CURRENT_APP.db_path.unlink()
    CURRENT_APP.init_db()
    try:
        workspace = ensure_workspace()
    except BranchError as error:
        return branch_error(error)
    clear_storefront_cookies(response)
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


# Registered LAST so it only catches paths no other route owns. Website apps such
# as the shopgym shops emit root-relative URLs (/assets/..., /collections/...,
# /images/...) that the iframe requests at the control-plane origin rather than
# under /runtime/. For those apps we forward any otherwise-unmatched path to the
# active runtime so the embedded storefront's assets and navigation resolve on a
# single origin (works through the Cloudflare/SSH tunnels too). db-backed apps
# (email/inventory) use relative URLs under /runtime/ and never reach this.
@app.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def runtime_root_fallback(request: Request, full_path: str) -> Response:
    if CURRENT_APP.db_backed:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    branch = running_workspace_branch()
    if not branch:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return await proxy_request_to_branch(request, branch, runtime_forward_path(request.url.path))
