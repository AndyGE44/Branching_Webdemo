# StateFork build-mode image for the mock Clothing shop.
#
# Waypoint builds this with `buildah bud`, so the base must be in the *root*
# container storage:  sudo podman load -i ~/shopgym/docker-images/shop-arena-mock-clothing.tar.gz
FROM localhost/shop-arena-mock-clothing:latest

# Prebundle the mock Storefront API (TypeScript) into a single plain-JS file so
# the API process is one plain `node`. CRIU cannot checkpoint the process when
# it runs under tsx: tsx's loader holds an inotify watch on a deleted file
# (/no-such-path) that CRIU fails to dump. Deps stay external and resolve from
# /app/mock-api/node_modules at runtime, so the bundle must live in that dir.
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
# (so the control panel embeds the Hydrogen UI, not the bare GraphQL API):
#   1. prebundled mock Storefront API (plain node) on :4000  -> CRIU-safe
#   2. Hydrogen storefront (plain `node server.mjs`) on $PORT, pointed at :4000
# This mirrors the image's own /app/entrypoint.sh but uses the prebundled API
# and a configurable port so the whole tree is checkpointable by Waypoint/CRIU.
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
