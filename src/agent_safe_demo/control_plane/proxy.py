"""Reverse proxy that serves the embedded storefront on the control-plane origin.

The storefront runs with ``basename=/runtime`` (see the shop Dockerfiles), but
Hydrogen still emits some root-relative URLs (``/assets/...``,
``/collections/...``). Serving everything through the control-plane origin —
``/runtime/*`` plus a catch-all for those root-relative paths — keeps the
embedded site working through the Cloudflare/SSH tunnels and keeps Basic Auth
in front of the shops too.
"""

from __future__ import annotations

from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

import anyio
from fastapi import Request
from fastapi.responses import JSONResponse, Response

RUNTIME_MOUNT = "/runtime"

# Paths the storefront serves at its ORIGIN ROOT rather than under the
# basename: Vite assets, product images, health, and other dist/client static
# files. Everything else root-relative is a document route and must be
# re-prefixed with the mount so the basename-aware server matches it.
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

# Cookies the embedded storefronts set on the control-plane origin to hold the
# in-memory cart id (and Hydrogen session). They persist in the browser across
# shop switches and resets, and because the mock-api assigns deterministic cart
# ids the same id is valid in the next shop — so the cart would appear to carry
# over. Clearing them when the workspace context changes gives each shop/reset
# a fresh cart.
STOREFRONT_COOKIES = ("cart", "session")


def clear_storefront_cookies(response: Response) -> None:
    for name in STOREFRONT_COOKIES:
        response.delete_cookie(name, path="/")


def runtime_forward_path(request_path: str) -> str:
    """Map an incoming control-plane path to the path to request on the runtime.

    ``/runtime/*`` documents and root static assets pass through unchanged; a
    stray root-relative document link (e.g. a hardcoded ``<a href="/pages/about">``)
    gets the mount prepended so the basename-aware server matches it."""
    if request_path == RUNTIME_MOUNT or request_path.startswith(RUNTIME_MOUNT + "/"):
        return request_path
    if request_path.startswith(RUNTIME_ROOT_PREFIXES):
        return request_path
    return RUNTIME_MOUNT + request_path


def _proxy_headers(request: Request, branch: dict) -> dict[str, str]:
    blocked = {
        "host",
        "content-length",
        "accept-encoding",
        "connection",
        # A fronting proxy (Tailscale Funnel, some CDN tunnels) adds
        # X-Forwarded-Host: <public host>. React Router's action guard compares
        # that against Origin — which we realign to the runtime just below — so
        # it can never match and every action POST (add-to-cart) 500s. Strip it
        # and the guard falls back to Host, which does match.
        "x-forwarded-host",
        # We proxy to branch.url, so urllib sends Host=<branch host>. React
        # Router's single-fetch action guard rejects a POST whose Origin host
        # != Host, which breaks add-to-cart through the control-plane origin —
        # so Origin is realigned to the runtime below (and Referer dropped).
        "origin",
        "referer",
    }
    headers = {
        key: value for key, value in request.headers.items() if key.lower() not in blocked
    }
    headers["Origin"] = branch["url"]
    return headers


def _proxied_response(content: bytes, status_code: int, upstream_headers) -> Response:
    media_type = upstream_headers.get("content-type") if upstream_headers is not None else None
    response = Response(content=content, status_code=status_code, media_type=media_type)
    if upstream_headers is not None:
        # Forward Set-Cookie (the Hydrogen session that holds the cart id — may
        # be several) and the response headers the embedded app relies on.
        # Hop-by-hop and length headers are managed by Starlette.
        for cookie in upstream_headers.get_all("set-cookie", []):
            response.raw_headers.append((b"set-cookie", cookie.encode("latin-1")))
        for key in ("location", "cache-control"):
            value = upstream_headers.get(key)
            if value:
                response.headers[key] = value
    return response


async def forward_to_branch(request: Request, branch: dict) -> Response:
    """Forward the incoming request to the branch runtime and relay the response."""
    if branch.get("checkpointing"):
        return JSONResponse({"detail": "Runtime is checkpointing; retry shortly."}, status_code=503)

    body = await request.body()
    query = request.scope.get("query_string", b"").decode("utf-8")
    forward_path = runtime_forward_path(request.url.path)
    target = f"{branch['url']}{forward_path}{'?' + query if query else ''}"
    proxy_request = urlrequest.Request(
        target,
        data=body or None,
        headers=_proxy_headers(request, branch),
        method=request.method,
    )

    def fetch() -> Response:
        try:
            with urlrequest.urlopen(proxy_request, timeout=30) as upstream:
                return _proxied_response(upstream.read(), upstream.status, upstream.headers)
        except HTTPError as error:
            return _proxied_response(error.read(), error.code, error.headers)
        except URLError as error:
            return JSONResponse({"detail": f"Runtime proxy failed: {error}"}, status_code=502)

    # urllib is blocking; run it in a worker thread so one slow upstream request
    # cannot stall the event loop (the storefront loads many assets in parallel).
    return await anyio.to_thread.run_sync(fetch)
