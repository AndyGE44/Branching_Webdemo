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

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent_safe_demo.control_plane.auth import require_basic_auth
from agent_safe_demo.control_plane.idle import ActivityTracker, run_idle_reset_monitor
from agent_safe_demo.control_plane.proxy import clear_storefront_cookies, forward_to_branch
from agent_safe_demo.control_plane.statefork import BranchError
from agent_safe_demo.control_plane.workspace import DATA_BACKEND, Workspace

logger = logging.getLogger("control_plane.main")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

workspace = Workspace()
# Tracks user "movement" and whether the shop has diverged from its clean build,
# so the idle monitor can auto-reset an abandoned-but-dirty demo (and skip a
# pristine one). See idle.py.
activity = ActivityTracker()

# Methods that only read the storefront (browsing, asset fetches). A write
# through the proxy — the cart LinesAdd/Remove POST — changes runtime state and
# so marks the workspace dirty.
READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _start_dolt_server():
    """Start the shared external dolt sql-server when the storefront data tier
    is enabled (DEMO_SHOP_DB_BACKEND=dolt_server). Best-effort: on any failure
    the demo falls back to the baked JSON catalog with no data tier."""
    if DATA_BACKEND != "dolt_server":
        return None
    try:
        from agent_safe_demo.control_plane.dolt_server import DoltSqlServer

        server = DoltSqlServer(
            Path(os.getenv("DEMO_SHOP_DOLT_DIR", str(Path.home() / "demo_shop_dolt"))).expanduser(),
            host=os.getenv("DEMO_SHOP_DOLT_HOST", "127.0.0.1"),
            port=int(os.getenv("DEMO_SHOP_DOLT_PORT", "3306")),
            dolt_bin=os.getenv("DEMO_DOLT_BIN", "dolt"),
        )
        server.start()
        workspace.attach_dolt_server(server)
        return server
    except Exception as error:
        logger.warning("dolt sql-server unavailable: %s; serving baked JSON catalog only", error)
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure each shop build dir has the mock-api overlay source before any image
    # is built (the Dockerfiles COPY it). Cheap + idempotent; runs always so the
    # shops build identically whether or not the Dolt data tier is enabled.
    try:
        from agent_safe_demo.control_plane.overlay_sync import sync_shop_overlay_sources

        sync_shop_overlay_sources()
    except Exception as error:
        logger.warning("mock-api overlay sync failed: %s", error)
    # Bring up the external Dolt data tier (if enabled) before serving traffic.
    dolt_server = _start_dolt_server()
    # Run the idle auto-reset loop for the life of the process.
    monitor = asyncio.create_task(run_idle_reset_monitor(workspace, activity))
    try:
        yield
    finally:
        monitor.cancel()
        try:
            await monitor
        except asyncio.CancelledError:
            pass
        if dolt_server is not None:
            dolt_server.stop()


app = FastAPI(
    title="Agent-Safe Workspace Controller",
    description="A StateFork web shell that snapshots and restores live shop runtimes.",
    version="0.2.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def track_activity(request: Request, call_next):
    # Every real request is "movement" that defers the idle reset. Registered
    # before the auth middleware below, so it sits INSIDE auth (the last-added
    # middleware is outermost in Starlette) and only authenticated requests
    # count; /healthz is ignored inside the tracker.
    activity.record_request(request.url.path)
    return await call_next(request)


app.middleware("http")(require_basic_auth)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class SnapshotRequest(BaseModel):
    label: str | None = Field(default=None, max_length=80)


class RestoreRequest(BaseModel):
    snapshot_id: str


def branch_error_response(error: BranchError) -> JSONResponse:
    return JSONResponse({"detail": str(error)}, status_code=400)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    """Unauthenticated liveness probe for supervisors. Does not count as demo
    movement (see idle.IGNORED_ACTIVITY_PATHS), so polling it will not keep the
    idle auto-reset from firing."""
    return {"status": "ok"}


@app.get("/api/apps")
def list_apps() -> dict:
    return workspace.apps_payload()


@app.post("/api/apps/{app_id}/select")
def select_app(app_id: str, response: Response) -> dict:
    try:
        result = workspace.select_app(app_id)
        # The new app starts from a fresh clean build.
        activity.mark_clean()
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
        result = workspace.snapshot(label=payload.label if payload else None)
        activity.mark_dirty()
        return result
    except BranchError as error:
        return branch_error_response(error)


@app.post("/api/workspace/restore")
def restore_snapshot(payload: RestoreRequest) -> dict:
    try:
        result = workspace.restore(payload.snapshot_id)
        activity.mark_dirty()
        return result
    except BranchError as error:
        return branch_error_response(error)


@app.post("/api/workspace/reset")
def reset_workspace(response: Response) -> dict:
    cleanup = workspace.reset()
    # The mutation is gone; the workspace is back at the original clean state
    # regardless of whether the eager rebuild below succeeds.
    activity.mark_clean()
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


class CatalogUpdateRequest(BaseModel):
    """A price/inventory edit against the external Dolt catalog (working set)."""

    price: float | None = Field(default=None, ge=0)
    compare_at_price: float | None = Field(default=None, ge=0)
    on_hand: int | None = Field(default=None, ge=0)


@app.get("/api/catalog")
def get_catalog(search: str | None = None) -> dict:
    """List the storefront's pricing/inventory (variant_state) plus the row-level
    Dolt diff vs the pristine catalog. 400 if the data tier is not enabled."""
    try:
        return workspace.catalog_list(search=search)
    except BranchError as error:
        return branch_error_response(error)


@app.post("/api/catalog/{variant_id}")
def update_catalog(variant_id: str, payload: CatalogUpdateRequest) -> dict:
    """Edit one variant's price/stock in the Dolt working set. A later snapshot
    commits it to a Dolt branch; restore/reset rolls it back."""
    try:
        result = workspace.catalog_update(
            variant_id,
            price=payload.price,
            compare_at_price=payload.compare_at_price,
            on_hand=payload.on_hand,
        )
        activity.mark_dirty()  # a data edit diverges the workspace from clean
        return result
    except BranchError as error:
        return branch_error_response(error)


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
    if request.method not in READ_ONLY_METHODS:
        activity.mark_dirty()  # e.g. a cart LinesAdd/Remove POST
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
    if request.method not in READ_ONLY_METHODS:
        activity.mark_dirty()
    return await forward_to_branch(request, branch)
