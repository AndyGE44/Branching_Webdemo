# Three placements of a Dolt data tier — benchmark (2026-06-22)

Same data engine (**Dolt**), three placements relative to the StateFork checkpoint
boundary. This isolates "where the DB sits" from "which engine," extending the
SQLite(B)-vs-external-Dolt(A) study.

| | placement | what versions the data | what the checkpoint captures |
|---|---|---|---|
| **#1 coupled** | Dolt repo files **inside** the checkpoint (no DB process) | StateFork **fs** snapshot (Waypoint, fs-only) | app fs incl. the whole repo |
| **#2 full-system** | `dolt sql-server` **inside** the sandbox | StateFork **CRIU** checkpoint | app + **DB-server memory** + repo |
| **#3 external (arch A)** | `dolt sql-server` **outside** | **Dolt's own** commit/branch/reset | only the (tiny) app |

Harness: `scripts/bench-three-structures.py`. Each snapshot follows a 200-row UPDATE
(the delta). Medians of k=3 after a warmup. App-tier Waypoint constant (added to #3):
snapshot 13.4 ms / restore 14.0 ms.

## Results

### Snapshot / restore latency (ms)

| rows | #1 snap | #1 rest | #3 snap | #3 rest | #2 snap | #2 rest |
|-----:|----:|----:|----:|----:|----:|----:|
| 1k   | 11.0 | 13.2 | 38.2 | 35.7 | 225.3 | 223.4 |
| 100k | 10.9 | 13.9 | 38.2 | 33.8 | 245.4 | 232.2 |
| 1M   | 14.6 | 15.0 | 36.7 | 35.6 | 323.3 | 268.0 |

### Per-snapshot storage

| rows | #1 (fs ≈ whole repo) | #3 (Dolt delta) | #2 (CRIU memory) | #2 (fs) |
|-----:|----:|----:|----:|----:|
| 1k   | 33 KB   | 36 KB | 30 MB | 54 KB |
| 100k | 1.73 MB | 51 KB | 40 MB | 3.9 MB |
| 1M   | 17.6 MB | 66 KB | 79 MB | 41 MB |

## Findings

1. **#1 coupled — cheapest checkpoint, but whole-repo storage.** Snapshot is a pure
   OverlayFS op (~11–15 ms, flat with size; no DB process). But each snapshot stores
   **~the whole repo** (17.6 MB at 1M) — a single dolt commit rewrites enough storage
   (conjoin) that OverlayFS copies up nearly the entire repo. So #1 behaves like
   arch B (whole-DB capture), just with Dolt's more compact on-disk format
   (17.6 MB vs SQLite's 76 MB at 1M, ~4×). Captures **no** DB in-memory state.

2. **#3 external (arch A) — the only one with true delta storage.** Dolt versions
   logically (commit + branch + reset), so per-snapshot storage is a flat **~66 KB**
   regardless of data size. Moderate latency (~37 ms = app-tier Waypoint + Dolt op).
   Best when you branch a large dataset often.

3. **#2 full-system — heaviest, but the only one that preserves a warm DB.** Snapshot
   ~225–323 ms (grows with the server's working set → bigger CRIU memory dump:
   30→79 MB) **plus** ~the whole repo on disk (up to 41 MB) ≈ **120 MB/snapshot at 1M**.
   It is also the most tool-invasive: CRIU-checkpointing a running `dolt sql-server`
   required (a) `GODEBUG=multipathtcp=0` (Go's MPTCP sockets are unsupported by CRIU),
   (b) disabling dolt telemetry (no external `:443` connection at checkpoint time), and
   (c) patching Waypoint to pass `criu --file-locks` (dolt holds DB file locks). In
   return it captures the server's **buffer pool, sessions, and open transactions**, and
   restores an exact running server (same PID).

## The key insight

**Dolt's "cheap delta" only materialises through Dolt's own commit/branch graph (#3).**
The moment Dolt lives *inside* the checkpoint, you are back to capturing the whole
thing — the repo files for #1, the repo **and** the server's memory for #2 — and the
delta advantage is lost. So the placement decision is really:

- want **branch-cheap, big-data versioning** → external Dolt (#3);
- want the **simplest, fastest checkpoint** and don't mind whole-DB storage → #1;
- need the **DB's warm in-memory state** captured atomically with the app → #2, and pay
  for it (latency, storage, and tool patches).

## Reproduce

```bash
sudo env PATH=$PWD/.venv/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  DEMO_STATEFORK_ROOT=/path/to/Andy_StateFork \
  .venv/bin/python scripts/bench-three-structures.py 1000 100000 1000000
```
Note: #2 requires a `waypoint` built with `criu --file-locks` (see the patch to
`Andy_StateFork`'s Waypoint).
