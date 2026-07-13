# mock-api overlay (external Dolt price/inventory) — canonical source

This is the **canonical, reviewed** copy of the storefront mock Storefront API
source, patched to read a price/inventory *overlay* from an external Dolt
database (architecture A: the web service runs inside Waypoint, the data tier
lives outside in a `dolt sql-server` on the host).

Files:

- `overlay.ts` — **new.** The Dolt overlay client: connects (lazily, per
  operation) to the host `dolt sql-server`, caches the `variant_state` table
  with a short TTL, exposes `getOverlayVariant()` for the builders, and an
  optional `decrementStock()` writer. All no-ops unless `SHOP_DOLT_*` env is set.
- `data.ts` — patched. `buildProductVariantNode` / `buildProductNode` overlay
  price, compare-at price, and availability (honouring stock) when present.
- `server.ts` — patched. The GraphQL context factory is async and refreshes the
  overlay once per request.
- `resolvers.ts` — patched. `cartLinesAdd` optionally decrements Dolt stock
  (gated on `SHOP_DOLT_DECREMENT_ON_ADD=1`).

Everything else (schema, types, the JSON loaders) comes unchanged from the shop
base image at `/app/mock-api/src/`; the shop Dockerfile `COPY`s these four files
over it and re-runs the existing esbuild bundle (mysql2 stays external, imported
lazily).

## How it reaches the shop image

The shop Dockerfiles build with the shop dir as the buildah context, so they
`COPY mock-api-overlay/` — a **generated** copy of this dir placed inside each
`app_plane/shop_*/` (gitignored). The control plane syncs it at startup
(`control_plane/overlay_sync.py`); regenerate manually with
`scripts/sync-mock-api-overlay.sh` after editing files here.

## Env (set by the control plane per selected shop)

`SHOP_DOLT_HOST`, `SHOP_DOLT_PORT`, `SHOP_DOLT_DB`, `SHOP_DOLT_USER`,
`SHOP_DOLT_PASSWORD`; optional `SHOP_DOLT_OVERLAY_TTL_MS` (default 1000),
`SHOP_DOLT_DECREMENT_ON_ADD` (default off).
