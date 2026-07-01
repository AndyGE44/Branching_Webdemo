"""FastAPI control plane for the shopgym StateFork demo.

Serves the static control-panel UI, the workspace API it calls (select app,
snapshot, restore, reset), and the reverse proxy that embeds the storefront on
this origin. The heavy lifting lives in the sibling modules:

- ``workspace``  — the single-workspace controller (app + base + branch)
- ``statefork``  — StateFork base/branch/snapshot backend
- ``proxy``      — storefront reverse proxy
- ``auth``       — optional Basic Auth middleware
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent_safe_demo.control_plane.auth import require_basic_auth
from agent_safe_demo.control_plane.proxy import clear_storefront_cookies, forward_to_branch
from agent_safe_demo.control_plane.statefork import BranchError
from agent_safe_demo.control_plane.workspace import Workspace

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="Agent-Safe Workspace Controller",
    description="A StateFork web shell that snapshots and restores live shop runtimes.",
    version="0.2.0",
)
app.middleware("http")(require_basic_auth)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

workspace = Workspace()


class SnapshotRequest(BaseModel):
    label: str | None = Field(default=None, max_length=80)


class RestoreRequest(BaseModel):
    snapshot_id: str


def branch_error_response(error: BranchError) -> JSONResponse:
    return JSONResponse({"detail": str(error)}, status_code=400)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/apps")
def list_apps() -> dict:
    return workspace.apps_payload()


@app.post("/api/apps/{app_id}/select")
def select_app(app_id: str, response: Response) -> dict:
    try:
        result = workspace.select_app(app_id)
        clear_storefront_cookies(response)
        return result
    except ValueError as error:
        return JSONResponse({"detail": str(error)}, status_code=404)
    except BranchError as error:
        return branch_error_response(error)


@app.get("/api/workspace")
def get_workspace() -> dict:
    try:
        return workspace.ensure()
    except BranchError as error:
        return branch_error_response(error)


@app.post("/api/workspace/snapshots")
def save_snapshot(payload: SnapshotRequest | None = None) -> dict:
    try:
        return workspace.snapshot(label=payload.label if payload else None)
    except BranchError as error:
        return branch_error_response(error)


@app.post("/api/workspace/restore")
def restore_snapshot(payload: RestoreRequest) -> dict:
    try:
        return workspace.restore(payload.snapshot_id)
    except BranchError as error:
        return branch_error_response(error)


@app.post("/api/workspace/reset")
def reset_workspace(response: Response) -> dict:
    cleanup = workspace.reset()
    try:
        payload = workspace.ensure()
    except BranchError as error:
        return branch_error_response(error)
    # The runtime was rebuilt from scratch; clear the browser-side cart cookie
    # so the fresh shop does not pick up the old cart id.
    clear_storefront_cookies(response)
    return {"status": "reset", "cleanup": cleanup, **payload}


@app.get("/api/backend")
def backend_status() -> dict:
    """Diagnostics: backend method, totals, and measured snapshot/restore timings."""
    return workspace.backend.status()


# The proxy routes accept every method and are not part of the JSON API, so
# they are excluded from the OpenAPI schema (/docs shows the real API only).
PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


@app.api_route("/runtime", methods=PROXY_METHODS, include_in_schema=False)
@app.api_route("/runtime/{path:path}", methods=PROXY_METHODS, include_in_schema=False)
async def runtime_proxy(request: Request, path: str = "") -> Response:
    try:
        branch = workspace.ensure()["branch"]
    except BranchError as error:
        return branch_error_response(error)
    return await forward_to_branch(request, branch)


# Registered LAST so it only catches paths no other route owns. The storefront
# emits root-relative URLs (/assets/..., /collections/...) that the iframe
# requests at the control-plane origin rather than under /runtime/; forward
# them to the active runtime so the embedded site resolves on a single origin
# (works through the Cloudflare/SSH tunnels too).
@app.api_route("/{full_path:path}", methods=PROXY_METHODS, include_in_schema=False)
async def runtime_root_fallback(request: Request, full_path: str) -> Response:
    # Never proxy API or static paths — an unknown /api/* route is a 404, not
    # a storefront page.
    if full_path == "api" or full_path.startswith(("api/", "static/")):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    branch = workspace.running_branch()
    if not branch:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return await forward_to_branch(request, branch)
