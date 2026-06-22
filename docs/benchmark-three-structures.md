# Placements of a Dolt data tier — benchmark (2026-06-22)

Same data engine (**Dolt**), different placements relative to the StateFork checkpoint,
each measured in **both process (fs-only) and build (CRIU) mode** where applicable, so
the structures can be compared under identical checkpoint machinery.

| | placement / mode | versioning | checkpoint captures |
|---|---|---|---|
| **#1 fs-only** | repo files in checkpoint, process mode | StateFork fs | repo files |
| **#1 build/CRIU** | repo files in checkpoint, build mode, no server | StateFork CRIU+fs | repo + idle-shell memory |
| **#3 fs-only** | `dolt sql-server` external, process-mode app | Dolt branches | (tiny app fs) |
| **#3 build/CRIU** | external server, **build-mode app (CRIU)** | Dolt branches | app memory (idle shell), data stays external |
| **#2 full-system** | `dolt sql-server` inside the sandbox, build mode | StateFork CRIU+fs | repo + **DB-server** memory |

Harness: `scripts/bench-three-structures.py`. Each snapshot follows a 200-row UPDATE.
Medians of k=3 after a warmup.

## Snapshot latency (ms)

| rows | #1 fs | #1 build | #3 fs | #3 build | #2 |
|-----:|----:|----:|----:|----:|----:|
| 1k   | 11.9 | 138.8 | 39.4 | 162.2 | 228.1 |
| 100k | 11.4 | 134.9 | 39.6 | 160.6 | 236.9 |
| 1M   | 11.0 | 138.0 | 40.8 | 166.1 | 317.7 |

## Restore latency (ms)

| rows | #1 fs | #1 build | #3 fs | #3 build | #2 |
|-----:|----:|----:|----:|----:|----:|
| 1k   | 13.2 | 190.1 | 37.4 | 212.4 | 221.1 |
| 100k | 16.4 | 189.3 | 35.6 | 210.8 | 232.0 |
| 1M   | 14.6 | 189.8 | 37.2 | 211.1 | 260.9 |

## Per-snapshot storage (MB)

| rows | #1 fs | #1 build | #3 fs | #3 build | #2 |
|-----:|----:|----:|----:|----:|----:|
| 1k   | 0.03 | 2.9 | 0.04 | 2.3 | 30.4 |
| 100k | 1.73 | 7.0 | 0.05 | 2.4 | 39.3 |
| 1M   | 17.6 | 44.8 | 0.07 | 2.4 | 119.3 |

(#1/#2 = criu memory + fs; #3 fs = Dolt delta only; #3 build = idle-shell criu + external delta.)

## Point-write throughput (data tier)

| #1 dolt CLI (process/query) | #2 / #3 dolt server (pooled) |
|----:|----:|
| 22 ops/s | 1,414 ops/s |

(~64×. This is the *access method* — CLI vs server — not the placement: #2 and #3 both use a
server. Connect-per-query vs pooled adds another ~35× on top; see `benchmark-pool`.)

## Findings

1. **Build mode levies a flat CRIU tax, independent of where the data is.** fs-only → build:
   snapshot 12 → 139 ms (#1) and 40 → 163 ms (#3); restore 14 → 190 ms (#1) and 37 → 211 ms
   (#3). The extra is CRIU dumping a ~2–3 MB **idle shell** — there's no useful memory to
   capture, you just pay for build mode.

2. **Among build-mode structures, keeping data external (#3-build) gives the smallest,
   flattest checkpoint.** Per-snapshot storage: **#3-build ~2.4 MB flat** (idle-shell memory +
   a ~66 KB external Dolt delta) vs **#1-build → 44 MB** (repo files in the fs) vs **#2 → 119 MB**
   (server memory + repo) at 1M. Same latency tier, very different storage — because #3's data
   never enters the checkpoint.

3. **#2 is the only one that captures the warm DB**, and it's the heaviest (snapshot grows
   228 → 318 ms; storage to ~119 MB at 1M) because it dumps the server's RAM (30 → 78 MB).
   It also needed the most tooling: `GODEBUG=multipathtcp=0`, dolt telemetry off, and a
   Waypoint `criu --file-locks` patch.

4. **#3 fs-only (arch A) remains the storage champion** at ~66 KB/snapshot flat (pure Dolt
   delta, no CRIU). The build-mode app checkpoint only adds value if the app itself has
   in-memory state worth preserving.

5. **Throughput is about access method, not placement.** A server (pooled) does ~1,414
   point-writes/s; the CLI (a process per query) does ~22 — ~64× slower. So #2 and #3 (both
   server-backed) are fast; a CLI/file-backed #1 is not, regardless of where its files sit.

## Picking a structure

- Branch big data often, minimal checkpoint, fast queries → **#3 (external Dolt + server)**;
  add build-mode only if the app has memory state worth keeping (#3-build).
- Simplest, fastest checkpoint, small data → **#1 fs-only**.
- Must capture the DB's **warm in-memory state** atomically with the app → **#2**, paying the
  latency/storage/tooling cost.

## Reproduce

```bash
sudo env PATH=$PWD/.venv/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  DEMO_STATEFORK_ROOT=/path/to/Andy_StateFork \
  .venv/bin/python scripts/bench-three-structures.py 1000 100000 1000000
```
#1-build / #2 / #3-build require a `waypoint` built with `criu --file-locks`.
