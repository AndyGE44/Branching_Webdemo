# StateFork build-mode image for the mock Cookware shop.
# Base must be in *root* container storage (Waypoint builds with buildah as root):
#   sudo podman load -i ~/shopgym/docker-images/shop-arena-mock-cookware.tar.gz
FROM localhost/shop-arena-mock-cookware:latest

# Prebundle the mock Storefront API to plain JS so the running process is one
# plain `node` (CRIU cannot checkpoint the tsx loader). See shop_clothing.
RUN /usr/local/lib/node_modules/tsx/node_modules/@esbuild/linux-x64/bin/esbuild \
        /app/mock-api/src/server.ts \
        --bundle --platform=node --format=cjs --packages=external \
        --outfile=/app/mock-api/mockapi.cjs \
    && test -s /app/mock-api/mockapi.cjs
