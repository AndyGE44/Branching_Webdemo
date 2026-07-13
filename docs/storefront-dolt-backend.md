# Storefront external-Dolt data tier (architecture A)

The storefront demo can version its **pricing and inventory** in an external
[Dolt](https://github.com/dolthub/dolt) database, branched in lockstep with
StateFork snapshot/restore/commit. This is **architecture A** — *the web service
runs inside Waypoint, the data tier lives outside* — applied to the real Shopify
Hydrogen storefront (not a toy app).

It is **opt-in** and off by default: without `DEMO_SHOP_DB_BACKEND=dolt_server`
the demo behaves exactly as before (all runtime state, including the cart, is
captured inside the CRIU checkpoint — "architecture B").

## What it demonstrates

A merchandising / pricing / inventory **what-if**, previewed on the live site:

```
open branch → edit prices/stock (catalog editor) → preview on the live storefront
→ Snapshot (commits the data to a Dolt branch) → diff shows exactly what changed
→ Restore/Reset rolls it back, or Commit promotes it to the shop's next start
```

The demo now shows **two** rewind mechanisms coordinated by one snapshot:
CRIU rewinds the *app* (cart, process tree) and Dolt rewinds the *data* (prices,
stock). Dolt is the right tool for the data half — real row-level versioning,
cheap per-experiment branches, and a native diff a memory checkpoint can't
express.

## How it works

```
Inside Waypoint (chroot, host netns, CRIU-checkpointed)     Outside (host)
┌───────────────────────────────────────────────┐          ┌──────────────────────────┐
│ Hydrogen storefront  :$PORT  ──►  mock-api :4000│          │ dolt sql-server          │
│                                   (graphql-yoga)│          │ 127.0.0.1:3306           │
│   reads price+stock overlay ─────────────────────── MySQL ─►  db = <shop_id>          │
│   (connect-per-request, env-gated)              │          │   table: variant_state   │
└───────────────────────────────────────────────┘          └──────────┬───────────────┘
                                                                       │ CALL DOLT_COMMIT/BRANCH/RESET
Control plane (host root uvicorn)  ── seeds, edits, versions ──────────┘  Dolt branch sf_<snapshotid>
```

- The shop runs in StateFork **build mode** under `chroot` with **no network
  namespace**, so `127.0.0.1:3306` inside the shop is the host's Dolt server
  (verified in the Waypoint source: no `CLONE_NEWNET`). CRIU dumps with
  `--tcp-established`, so the mock API connects **per request** (no long-lived
  socket at checkpoint time).
- **Overlay model:** the bulk catalog (titles, images, collections) stays in the
  baked JSON; Dolt holds only the mutable, branchable fields in one table:

  ```sql
  CREATE TABLE variant_state (
    variant_id VARCHAR(64) PRIMARY KEY,  -- raw numeric mock-api variant id
    product_handle VARCHAR(255), product_title VARCHAR(255), variant_title VARCHAR(255),
    sku VARCHAR(255),
    price DECIMAL(10,2), compare_at_price DECIMAL(10,2),
    on_hand INT, available TINYINT(1)
  );
  ```

  seeded from each shop's `products.json` (`on_hand` synthetic — absent from the
  catalog). The mock API overlays these onto the JSON-built product nodes.
- **Lockstep versioning:** a StateFork snapshot runs `DOLT_ADD -A` +
  `DOLT_COMMIT` + `DOLT_BRANCH sf_<id>`; a restore runs `DOLT_CHECKOUT main` +
  `DOLT_RESET --hard sf_<id>` — both inside the existing checkpoint quiesce gate
  (`control_plane/statefork.py`).

### Components

| Concern | Location |
|---|---|
| Dolt sql-server lifecycle | `control_plane/dolt_server.py` (`DoltSqlServer`) |
| Versioning + summary/fingerprint | `control_plane/data_tier.py` (`DoltServerDataTier`) |
| Schema + seed + editor ops + diff | `control_plane/catalog.py` (`CatalogStore`) |
| Lockstep hooks | `control_plane/statefork.py` (`_data_tier_snapshot/_restore/_cleanup`) |
| Provisioning per shop | `control_plane/workspace.py` (`provision_data_tier`) |
| Server start/stop + catalog API | `control_plane/main.py` |
| Mock-api overlay (the app change) | `app_plane/mock_api_overlay/` (`overlay.ts` + patched `data.ts`/`server.ts`/`resolvers.ts`) |
| Catalog editor UI | `control_plane/static/` |

The mock-api overlay is synced into each shop build dir at control-plane startup
(`control_plane/overlay_sync.py`) so the shop Dockerfiles' `COPY mock-api-overlay/`
works; the shop image installs `mysql2` and re-bundles.

## Running it (VM / process-with-CRIU node)

```bash
# In .env (or the environment):
DEMO_SHOP_DB_BACKEND=dolt_server
DEMO_SHOP_DOLT_DIR=/users/alexxjk/demo_shop_dolt   # server base dir, one db per shop
DEMO_SHOP_DOLT_PORT=3306
DEMO_DOLT_BIN=/usr/local/bin/dolt                  # sudo PATH may not see a user-local dolt

./scripts/run-shopgym-statefork.sh
```

The control plane starts the `dolt sql-server`, seeds the selected shop's
database from `$SHOPGYM_DIR/mock_<name>/data/products.json`, and injects
`SHOP_DOLT_*` into the shop runtime. The UI gains a **Catalog data** panel
(shown only when the tier is live) → **Edit** opens the price/stock editor.

Try: edit a price + stock → watch the live storefront change → **Snapshot** →
edit again → **Restore** the first snapshot (site *and* data roll back) → the
editor's diff shows exactly which variants changed → **Reset** returns to the
pristine catalog.

`GET /api/catalog` and `POST /api/catalog/{variant_id}` back the editor;
`GET /api/backend` reports timings.

## Constraints / caveats

- **Requires the VM** (StateFork/Waypoint/CRIU). On a plain dev box the shop
  can't be checkpointed; the control plane still runs UI-only, and with the tier
  disabled the storefront serves the baked JSON.
- **`dolt` must be reachable by the (root) control plane** — install it
  system-wide or set `DEMO_DOLT_BIN` to an absolute path.
- **Build-time network** for `npm install mysql2` in the shop image. It is
  imported lazily, so a shop still builds and serves JSON if the install fails;
  the Dolt overlay is simply unavailable. For offline builds, vendor `mysql2`
  into `app_plane/mock_api_overlay/node_modules`.
- **Single active shop**: one server hosts one database per shop; switching
  shops re-provisions (seed once, then start from the pristine `clean` baseline).

## Testing

`tests/test_storefront_dolt.py` covers the no-DB logic always, and the live Dolt
roundtrip (seed/list/update/diff + snapshot/restore versioning) when a `dolt`
binary is on PATH (skipped otherwise).
