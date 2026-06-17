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

## Running the full control-plane UI on architecture A

The control plane is now Dolt-aware. When the inventory app's data backend is
`dolt`, `StateForkBackend`:

- routes dirty/diff/fingerprint/summary through a `DoltDataTier`
  (`control_plane/data_tier.py`) instead of reading a SQLite file, and
- passes `dolt_repo=` into `create_env_manager`, so the StateFork manager's
  `snapshot()`/`restore()` commit + branch + reset the external Dolt repo **in
  lockstep** with the app checkpoint (via `Andy_StateFork`'s `DoltController`).

Start the controller with the Dolt backend selected (process runtime mode):

```bash
cd Branching_Webdemo
. .venv/bin/activate
export PATH="$HOME/.local/bin:$PATH"             # dolt
export DEMO_APP_ID=inventory
export DEMO_INVENTORY_DB_BACKEND=dolt
export DEMO_INVENTORY_DOLT_DIR="$HOME/demo_inventory_dolt"   # OUTSIDE any checkpoint workdir
export DEMO_STATEFORK_ROOT=/path/to/Andy_StateFork
python -m uvicorn agent_safe_demo.control_plane.main:app --host 127.0.0.1 --port 8000
```

Both the control plane and the in-runtime inventory app read the same
`DEMO_INVENTORY_*` vars, so they share one external Dolt repo. The UI's
snapshot / restore / commit / diff now operate on that Dolt tier.

### ⚠️ Constraints / caveats

- **Process runtime mode only.** Architecture A needs the in-runtime app to
  reach the external host Dolt dir + `dolt` binary. StateFork's
  docker-build / `checkpoint_exec` modes run the app in an isolated environment
  (the runtime env even forces the DB path to `/<name>`), so the external repo
  is not reachable there. Use the default `runtime_type: process` manifest path.
- **Keep `DEMO_INVENTORY_DOLT_DIR` outside the branch workdir** or the checkpoint
  would capture it too, defeating the split.
- **Set `DEMO_INVENTORY_DOLT_DIR` explicitly** so the control plane and the app
  agree on the repo location (both default to `<db stem>_dolt`, but being
  explicit avoids surprises).

### What was verified vs. not

Unit-tested against real Dolt (no FastAPI / checkpoint-lite needed):
`DoltDataTier` summary/fingerprint, and `StateForkBackend`'s data-tier routing
(summary/fingerprint/dirty + the lockstep `dolt_repo` kwarg), plus that the
default `sqlite` path is unchanged. The **full UI flow** (checkpoint-lite
driving `manager.snapshot()/restore()` + the runtime process reaching the host
Dolt repo) must be validated on the VM, since checkpoint-lite/CRIU is not
available in every dev environment.

## Dolt sql-server backend (`dolt_server`) — realistic steady-state perf

The `dolt` backend spawns one `dolt` process per query, which is fine for
correctness but useless for throughput numbers. `DEMO_INVENTORY_DB_BACKEND=dolt_server`
runs the external Dolt repo as a long-lived **`dolt sql-server`** over the MySQL
protocol instead:

- the app store (`DoltServerInventoryStore`) connects with **PyMySQL + bind
  parameters** (no string-literal interpolation), and
- the control plane manages the server lifecycle (`control_plane/dolt_server.py`)
  and versions data **server-natively** via `CALL DOLT_ADD/COMMIT/BRANCH/RESET`
  (`DoltServerDataTier`) — the CLI `DoltController` is **not** used here, because
  running CLI write commands against a live server would fight its in-memory
  working set.

Run the UI on it (process runtime mode):

```bash
export DEMO_APP_ID=inventory
export DEMO_INVENTORY_DB_BACKEND=dolt_server
export DEMO_INVENTORY_DOLT_DIR="$HOME/demo_inventory_dolt"   # repo dir; db name = its basename
export DEMO_INVENTORY_DOLT_PORT=3306                          # control plane starts the server here
export DEMO_STATEFORK_ROOT=/path/to/Andy_StateFork
export PATH="$HOME/.local/bin:$PATH"
python -m uvicorn agent_safe_demo.control_plane.main:app --host 127.0.0.1 --port 8000
```

The control plane starts the server in its lifespan, seeds it, and exports
`DEMO_INVENTORY_DOLT_HOST/PORT/DB` so the in-runtime app connects to the same
server. Standalone proof:

```bash
python scripts/inventory-dolt-server-demo.py
```

Caveat: with a running server, snapshot/restore no longer go through the CLI, so
`DoltServerDataTier.statefork_kwargs()` is empty and the control plane calls
`on_snapshot`/`on_restore` explicitly at each StateFork checkpoint.

## Notes / limitations

- The `dolt` (CLI) backend uses `dolt sql -q` with quoted string literals (no
  bind parameters) — correctness-first. The `dolt_server` backend supersedes it
  for performance: PyMySQL bind parameters + a long-lived server.
- SQL dialect differences handled: `AUTOINCREMENT`→`AUTO_INCREMENT`,
  `TEXT PRIMARY KEY`→`VARCHAR`, `lastrowid`→`SELECT MAX(id)`, explicit
  `GROUP BY` of all non-aggregated columns.
