# StateFork build-mode image for the mock Cookware shop.
# Base must be in *root* container storage (Waypoint builds with buildah as root):
#   sudo podman load -i ~/shopgym/docker-images/shop-arena-mock-cookware.tar.gz
FROM localhost/shop-arena-mock-cookware:latest

# Prebundle the mock Storefront API to plain JS so the API process is one plain
# `node` (CRIU cannot checkpoint the tsx loader). See shop_clothing.
# --- External Dolt price/inventory overlay (architecture A) -----------------
# Layer the overlay source over the baked mock-api and install its DB client.
# The overlay is inert unless SHOP_DOLT_* env is set at runtime (mysql2 is
# imported lazily), so the default no-Dolt storefront is unchanged. The
# mock-api-overlay/ dir is synced from the canonical app_plane/mock_api_overlay/
# by the control plane at startup. mysql2 is pure-JS; install needs build-time
# network (vendor it into mock-api-overlay/node_modules for offline builds).
COPY mock-api-overlay/ /app/mock-api/src/
RUN cd /app/mock-api \
    && (npm install --no-save mysql2 \
        || echo "WARN: mysql2 install failed; the Dolt overlay will be unavailable")
# ---------------------------------------------------------------------------

RUN /usr/local/lib/node_modules/tsx/node_modules/@esbuild/linux-x64/bin/esbuild \
        /app/mock-api/src/server.ts \
        --bundle --platform=node --format=cjs --packages=external \
        --outfile=/app/mock-api/mockapi.cjs \
    && test -s /app/mock-api/mockapi.cjs

# Make the Hydrogen server honor a configurable React Router basename so the
# storefront can be embedded under the control plane's /runtime/ mount. Server
# and client must agree on the basename or client-side navigation falls through
# to the splat route ("Oops 500 — No result found for routeId routes/$"). It
# defaults to "/" (so the image still works standalone); run-shop.sh sets /runtime.
RUN sed -i 's#^    build,$#    build: {...build, basename: process.env.RUNTIME_BASENAME || "/"},#' /app/hydrogen/server.mjs \
    && grep -q 'RUNTIME_BASENAME' /app/hydrogen/server.mjs

# Launcher that serves the actual storefront WEBSITE as one managed process tree
# (prebundled mock API on :4000 + Hydrogen `node server.mjs` on $PORT). See
# shop_clothing for the full rationale.
RUN { \
      echo '#!/bin/sh'; \
      echo 'set -eu'; \
      echo 'PORT="${1:-3000}"'; \
      echo 'DATA_DIR=/app/data'; \
      echo '[ -d "$DATA_DIR" ] || DATA_DIR=/app/raw'; \
      echo 'UV_USE_IO_URING=0 node /app/mock-api/mockapi.cjs "$DATA_DIR" 4000 &'; \
      echo 'MOCK_PID=$!'; \
      echo 'i=0; while [ "$i" -lt 60 ]; do'; \
      echo '  curl -sf http://localhost:4000/health >/dev/null 2>&1 && break'; \
      echo '  i=$((i+1)); sleep 0.5'; \
      echo 'done'; \
      echo 'cd /app/hydrogen'; \
      echo 'export PORT NODE_ENV=production UV_USE_IO_URING=0'; \
      echo 'export PUBLIC_STORE_DOMAIN=http://localhost:4000'; \
      echo 'export RUNTIME_BASENAME="${RUNTIME_BASENAME:-/runtime}"'; \
      echo 'node server.mjs &'; \
      echo 'HYDRO_PID=$!'; \
      echo '# Stop BOTH the mock API and the storefront together so the mock-api'; \
      echo '# (in-memory cart state on :4000) never orphans across a stop, which'; \
      echo '# would otherwise keep serving a stale cart through snapshot/restore/reset.'; \
      echo 'trap "kill -TERM $MOCK_PID $HYDRO_PID 2>/dev/null || true" TERM INT'; \
      echo 'trap "kill -KILL $MOCK_PID $HYDRO_PID 2>/dev/null || true" EXIT'; \
      echo 'wait "$HYDRO_PID"'; \
    } > /app/run-shop.sh \
    && chmod +x /app/run-shop.sh

# Runtime command comes from statefork.yaml:
#   bash /app/run-shop.sh ${PORT}
