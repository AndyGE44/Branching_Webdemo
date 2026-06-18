# Architecture A — Dolt data tier — Handoff / Status

This document records what was built to make the demo run **architecture A**
(StateFork checkpoints the **app tier**; an **external Dolt database** is the
**data tier**, branched by Dolt's own versioning), how to run/verify it, and
what remains to do on a node that has **CRIU** (for the StateFork app-tier
checkpoint).

Status date: 2026-06-17.

---

## TL;DR

- **Data tier (Dolt) is fully implemented, tested, and runnable on this VM.**
  Three interchangeable inventory backends: `sqlite` (arch B), `dolt` (arch A,
  CLI), `dolt_server` (arch A, long-lived `dolt sql-server`).
- The control-plane UI is wired so snapshot / restore / commit / diff operate on
  the Dolt data tier in lockstep with the app checkpoint.
- **App tier (StateFork/Waypoint) now runs on a CRIU-capable node.** CRIU 4.2
  (criu PPA) + the prebuilt Waypoint v0.6.0 binary are present, and the full UI
  flow was driven end-to-end with **both tiers moving in lockstep**: a
  snapshot/restore through the control-plane API rolls the Dolt data tier
  back/forward together with the app checkpoint. See "Verified run on the CRIU
  node" for the exact config.

## Two repos / branches involved

| Repo | Branch | Role |
|------|--------|------|
| `Branching_Webdemo` | `feature/inventory-dolt-backend` | App + control plane + Dolt data tiers (this doc) |
| `Andy_StateFork` | `feature/external-dolt-control` | `DoltController` (used by the **`dolt` CLI** backend's lockstep) |

`Andy_StateFork` must be checked out on the same machine and pointed to via
`DEMO_STATEFORK_ROOT`. The `dolt_server` backend does **not** use
`DoltController` (it versions server-natively); only the `dolt` (CLI) backend
does, via the StateFork manager's `dolt_repo` kwarg.

## Architecture

```
 Browser ──▶ Control Plane (FastAPI :8000, control_plane/main.py)
              ├─ StateFork  → checkpoints the APP PROCESS (app tier)   ← needs CRIU
              ├─ runs the inventory app as a child runtime (process mode)
              └─ data tier via DataTier:
                    sqlite      → file inside the checkpoint            (arch B)
                    dolt        → external repo, CLI per query          (arch A)
                    dolt_server → external repo, long-lived sql-server  (arch A)

 snapshot(id) = StateFork checkpoint(app)  +  Dolt commit + branch sf_<id>
 restore(id)  = StateFork restore(app)     +  Dolt reset --hard sf_<id>
 (paired at the 6 snapshot/restore sites in control_plane/branching.py)
```

## What was implemented

### `Andy_StateFork` (branch `feature/external-dolt-control`)
- `controller/dolt_controller.py` — `DoltController`: versions an external Dolt
  repo via the `dolt` CLI (`add`/`commit`/`branch -f`/`reset --hard`). Self-
  disables if `dolt` is absent; failures are logged, never fatal.
- `controller/base_env_manager.py` — `snapshot()/restore()/cleanup()` drive the
  attached `DoltController` for both physical and virtual snapshots; a Dolt
  failure is logged but the file-system snapshot/restore still reports success.
- Factory attaches a `DoltController` when `dolt_repo=` is passed.

### `Branching_Webdemo` (branch `feature/inventory-dolt-backend`)
- `app_plane/inventory_service/store.py` — `InventoryStore` interface with:
  - `SqliteInventoryStore` (arch B, unchanged behaviour, **default**),
  - `DoltInventoryStore` (arch A, CLI `dolt sql -q`),
  - `DoltServerInventoryStore` (arch A, PyMySQL + bind params to a sql-server).
  - `app.py` is backend-agnostic; selection via `DEMO_INVENTORY_DB_BACKEND`.
- `control_plane/data_tier.py` — `DataTier` strategy:
  - `DoltDataTier` (CLI): summary/fingerprint via `dolt sql`, lockstep via the
    StateFork manager's `dolt_repo`.
  - `DoltServerDataTier`: summary/fingerprint + versioning over MySQL using
    `CALL DOLT_ADD/COMMIT/BRANCH/RESET`; `statefork_kwargs()` is empty (no CLI).
  - `on_snapshot`/`on_restore` hooks (no-op for sqlite/CLI; real for server).
- `control_plane/dolt_server.py` — `DoltSqlServer` lifecycle (start/attach,
  ready-wait, db discovery, stop, conn params).
- `control_plane/branching.py` — `StateForkBackend` takes `data_backend` /
  `dolt_dir` / `server_params`; routes fingerprint/summary/dirty/diff through
  the tier; calls `on_snapshot`/`on_restore` at the 6 StateFork checkpoint sites;
  guards the SQLite-file checks. **`sqlite` path is byte-for-byte unchanged.**
- `control_plane/main.py` — `data_backend_config()` resolves the backend and the
  external Dolt dir / server params from env; lifespan starts/stops the
  `dolt sql-server` and exports its connection env to the in-runtime app.
- `scripts/inventory-dolt-ab-demo.py` (CLI) and
  `scripts/inventory-dolt-server-demo.py` (server) — standalone arch-A proofs.

## What is verified vs. not

**Verified on this VM (real `dolt` 2.1.6, real PyMySQL):**
- `DoltController` end-to-end (CLI): snapshot/mutate/restore rolls the DB back/fwd.
- `DoltInventoryStore` and `DoltServerInventoryStore` parity with SQLite.
- `DoltServerDataTier` server-native snapshot/restore (CALL DOLT_*) rollback.
- `StateForkBackend` data-tier routing (summary/fingerprint/dirty + the lockstep
  hooks) for both CLI and server tiers; `sqlite` default unchanged.
- `pytest tests/test_api.py -k "inventory or kv or mailbox_seed"` → 4 passed.
- `control_plane.main` imports cleanly; `controller` imports in the venv.

**Verified on the CRIU node (2026-06-17):**
- The full UI flow with real StateFork **app-tier checkpoints** (Waypoint/CRIU):
  workspace → Run Agent → Snapshot → edit → Restore via the control-plane API,
  with the Dolt data tier rolling back/forward in lockstep. Confirmed two ways —
  inventory counts/items returned to the snapshot's values, and the external repo
  carried a per-snapshot `sf_<id>` branch (`dolt branch` in the Dolt dir).

## Environment already set up on this VM

- `dolt` 2.1.6 at `~/.local/bin/dolt` (on PATH).
- `python3.12-venv` + `pip` (apt).
- venv at `Branching_Webdemo/.venv` with the package installed
  (`pip install -e ".[dev]"`): fastapi, uvicorn, **PyMySQL**, psutil, paramiko,
  PyYAML, pytest.

Run the data-tier proofs now:
```bash
cd Branching_Webdemo && . .venv/bin/activate && export PATH="$HOME/.local/bin:$PATH"
python scripts/inventory-dolt-ab-demo.py        # arch A, CLI (needs DEMO_STATEFORK_ROOT)
python scripts/inventory-dolt-server-demo.py    # arch A, sql-server
```

## Verified run on the CRIU node (2026-06-17)

This node (CloudLab, Ubuntu 24.04, kernel 6.8) already had the prerequisites the
old VM lacked — plus two regressions to repair:

- **CRIU 4.2** from the criu PPA (`/usr/sbin/criu`; `sudo criu check` → "Looks
  good"). Unprivileged use needs `CAP_CHECKPOINT_RESTORE`; we just run the
  control plane under `sudo`, since Waypoint (CRIU + OverlayFS) needs root anyway.
- **Waypoint v0.6.0** binary already built at `Andy_Waypoint/waypoint`, symlinked
  from `Andy_StateFork/waypoint`.
- Regressions vs. the old VM: `dolt` was gone (reinstalled **2.1.8** to
  `/usr/local/bin`, identity set for both users) and the venv was gone (recreated
  from Python 3.13 via `pip install -e ".[dev]"`).

**Process runtime mode is required** — the doc's earlier `criu_build` guess does
not fit the branching flow (CRIUBuildManager launches/own-checkpoints its own
process and has no `exec`). The inventory manifest shipped as
`runtime.type: checkpoint_exec`, which isolates the app (DB forced to `/<name>`,
run via `waypoint exec` inside a Dockerfile env that has no PyMySQL). Arch A needs
the app on the host so it can reach the external Dolt repo, so change
`app_plane/inventory_service/statefork.yaml`:

    runtime:
      type: process            # was: checkpoint_exec
      cwd: "${BRANCH_WORKDIR}"  # was: "/"

With `runtime.type: process`, `ckpt_build` resolves to
`WaypointBuildManager(build=False)` → `waypoint init` (a filesystem checkpoint of
the work_dir); the app is launched on the host by `RuntimeProcessManager`; and the
manager's `snapshot()/restore()` drive the attached `DoltController` (CLI `dolt`)
in lockstep.

Launch (root for Waypoint; put the venv on PATH so the app subprocess and `dolt`
resolve):

```bash
sudo env \
  DEMO_APP_ID=inventory \
  DEMO_INVENTORY_DB_BACKEND=dolt \
  DEMO_INVENTORY_DOLT_DIR=/users/alexxjk/demo_inventory_dolt \
  DEMO_STATEFORK_ROOT=/users/alexxjk/Andy_StateFork \
  DEMO_STATEFORK_METHOD=ckpt_build \
  DEMO_DOLT_BIN=dolt \
  PATH=/users/alexxjk/Branching_Webdemo/.venv/bin:/usr/local/bin:/usr/bin:/bin \
  /users/alexxjk/Branching_Webdemo/.venv/bin/python -m uvicorn \
  agent_safe_demo.control_plane.main:app --host 127.0.0.1 --port 8000
```

Then drive Run Agent → Snapshot → edit → Restore (browser, or the API:
`/api/workspace`, `/api/workspace/run-agent`, `/api/workspace/snapshots`,
`/api/workspace/restore`; runtime proxied under `/runtime`). Observed: inventory
counts and each item's available/reserved rolled back with the app checkpoint, and
the external repo gained an `sf_<snapshot_id>` branch per snapshot.

`dolt_server` also works in process mode (started in the FastAPI lifespan; the app
uses PyMySQL over `127.0.0.1:3306` — Waypoint does **not** namespace the network).
Prefer it for benchmarks; the CLI `dolt` backend spawns a process per query.

## Constraints / gotchas

- **Process runtime mode only.** Architecture A needs the in-runtime app to reach
  the external host Dolt dir + `dolt` binary. StateFork docker-build /
  `checkpoint_exec` modes isolate the app (the runtime env even forces the DB
  path to `/<name>`), so the external repo is unreachable there.
- **Keep `DEMO_INVENTORY_DOLT_DIR` outside the branch/checkpoint workdir**, or the
  app checkpoint would capture it and defeat the split.
- **Stateless-app note.** `inventory` keeps all state in Dolt, so its app-tier
  checkpoint is largely redundant — architecture A's value is concentrated in the
  data tier. We run it in **process** runtime mode (`waypoint init`, a
  filesystem checkpoint of the work_dir) with the app on the host; the Dolt data
  tier does the real versioning. This is the no-op-ish/process variant the doc
  previously deferred.
- **CLI vs server.** `dolt` (CLI) spawns a process per query — fine for
  correctness, useless for throughput. Use `dolt_server` for any benchmark
  numbers.

## Suggested next steps

- A-vs-B(+server) **benchmark**: sweep data sizes (1k / 100k / 1M rows), measure
  snapshot/restore latency, steady-state throughput, and storage; find the
  crossover where A overtakes B.
- Optional: add the **no-op/process StateFork backend** so the stateless-app
  variant of A runs without CRIU.
