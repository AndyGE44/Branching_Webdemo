# Inventory Dolt backend (architecture A)

The inventory app can run its data tier on either of two backends, selected by
`DEMO_INVENTORY_DB_BACKEND`:

| Backend | Data tier | Branching of data | Mapping to StateFork |
|---------|-----------|-------------------|----------------------|
| `sqlite` (default) | SQLite file in the branch workdir | none — the file is captured *inside* the checkpoint | **Architecture B**: StateFork snapshots the app process **and** the SQLite file together |
| `dolt` | external Dolt repo (host dir) the app talks to via the `dolt` CLI | Dolt's own branches, one per snapshot id (`sf_<id>`) | **Architecture A**: StateFork checkpoints only the app; the data tier is versioned by `Andy_StateFork`'s `DoltController` |

The split lives entirely in `inventory_service/store.py`
(`SqliteInventoryStore` / `DoltInventoryStore` behind a common `InventoryStore`
interface). `app.py` is backend-agnostic; only the `store` and a few SQL-dialect
details differ. The two backends are behavior-compatible (same seed data, same
`/api/*` responses).

## How architecture A works

1. The app opens a fresh connection **per request** and writes land in the Dolt
   repo's *working set* (not a Dolt commit).
2. On a StateFork `snapshot(id)`, `DoltController` runs `dolt add -A` +
   `dolt commit` and points branch `sf_<id>` at the new commit.
3. On a StateFork `restore(id)`, `DoltController` resets the working branch
   (`dolt reset --hard sf_<id>`); the app's next request reads the rolled-back
   data.

Because the app uses per-request connections and the controller quiesces /
restarts the runtime around snapshots (see `control_plane/branching.py`), there
is no long-lived DB connection to invalidate — which is why the simple
single-active-branch (linear) model fits without a multi-branch `dolt
sql-server`.

## Try it

```bash
# Requires `dolt` on PATH and StateFork checked out.
DEMO_STATEFORK_ROOT=/path/to/Andy_StateFork \
    python scripts/inventory-dolt-ab-demo.py
```

Expected: seed → `snapshot(base)` → agent edits → `snapshot(v1)` →
`restore(base)` rolls the data back → `restore(v1)` rolls it forward.

To run the app itself on Dolt:

```bash
export DEMO_INVENTORY_DB_BACKEND=dolt
export DEMO_INVENTORY_DOLT_DIR=/tmp/demo_inventory_dolt   # outside the checkpoint
python -m uvicorn agent_safe_demo.app_plane.inventory_service.app:app --port 8300
```

## Not yet wired (next step)

The control plane's dirty/diff/fingerprint logic
(`control_plane/branching.py`) still reads the SQLite file directly
(`sqlite_fingerprint`, `_read_summary`). For a full UI demo on Dolt, that layer
should switch to `dolt diff` / `dolt status` and the base/branch/commit/discard
lifecycle should drive `DoltController` directly. The current change keeps the
app + data tier (architecture A) provable in isolation via the script above.

## Notes / limitations

- The Dolt backend uses the `dolt sql -q` CLI with quoted string literals (no
  bind parameters). This is the correctness-first path; the benchmark path
  should move to `dolt sql-server` + a driver for realistic throughput numbers.
- SQL dialect differences handled: `AUTOINCREMENT`→`AUTO_INCREMENT`,
  `TEXT PRIMARY KEY`→`VARCHAR`, `lastrowid`→`SELECT MAX(id)`, explicit
  `GROUP BY` of all non-aggregated columns.
