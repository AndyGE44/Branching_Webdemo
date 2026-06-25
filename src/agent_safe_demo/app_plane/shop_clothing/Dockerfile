# StateFork build-mode image for the mock Clothing shop.
#
# Waypoint builds this with `buildah bud`, so the base must be in the *root*
# container storage:  sudo podman load -i ~/shopgym/docker-images/shop-arena-mock-clothing.tar.gz
FROM localhost/shop-arena-mock-clothing:latest

# Prebundle the mock Storefront API (TypeScript) into a single plain-JS file so
# the running process is one plain `node`. CRIU cannot checkpoint the process
# when it runs under tsx: tsx's loader holds an inotify watch on a deleted file
# (/no-such-path) that CRIU fails to dump. Deps stay external and resolve from
# /app/mock-api/node_modules at runtime, so the bundle must live in that dir.
RUN /usr/local/lib/node_modules/tsx/node_modules/@esbuild/linux-x64/bin/esbuild \
        /app/mock-api/src/server.ts \
        --bundle --platform=node --format=cjs --packages=external \
        --outfile=/app/mock-api/mockapi.cjs \
    && test -s /app/mock-api/mockapi.cjs

# Runtime command comes from statefork.yaml:
#   node /app/mock-api/mockapi.cjs /app/data ${PORT}
