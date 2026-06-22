# Placements of a Dolt data tier — benchmark (2026-06-22)

Same data engine (**Dolt**), different placements relative to the StateFork checkpoint
boundary. Extends the SQLite(B)-vs-external-Dolt(A) study and adds a **fair control**:
#1 measured in *both* process (fs-only) and build (CRIU) mode, so #1 and #2 can be
compared under the identical checkpoint machinery.

| | placement / mode | versioning | checkpoint captures |
|---|---|---|---|
| **#1 coupled, fs-only** | Dolt repo files in the checkpoint, process mode (`waypoint init`) | StateFork fs | repo files (no memory) |
| **#1 coupled, build/CRIU** | same files, build mode (`waypoint build` + CRIU), **no DB server** | StateFork CRIU+fs | repo files + **idle shell** memory |
| **#2 full-system** | `dolt sql-server` **inside** the sandbox, build mode | StateFork CRIU+fs | repo + **DB-server** memory |
| **#3 external (arch A)** | `dolt sql-server` **outside** | **Dolt's own** branches | only the (tiny) app |

Harness: `scripts/bench-three-structures.py` (self-contained — builds its own dolt
sandbox image). Each snapshot follows a 200-row UPDATE. Medians of k=3 after a warmup.

## Snapshot latency (ms)

| rows | #1 fs-only | #1 build/CRIU | #3 external | #2 full-system |
|-----:|----:|----:|----:|----:|
| 1k   | 11.9 | 140.6 | 39.7 | 231.9 |
| 100k | 12.3 | 136.9 | 39.9 | 241.9 |
| 1M   | 12.3 | 138.1 | 41.4 | 321.2 |

## Per-snapshot storage (CRIU memory + filesystem)

| rows | #1 fs-only (fs) | #1 build/CRIU (mem + fs) | #3 external (Dolt delta) | #2 full-system (mem + fs) |
|-----:|----:|----:|----:|----:|
| 1k   | 33 KB   | 3.0 MB (3.0 + 0.05) | **36 KB** | 35 MB (35 + 0.05) |
| 100k | 1.73 MB | 6.7 MB (2.8 + 3.9) | **51 KB** | 40 MB (36 + 3.9) |
| 1M   | 17.6 MB | 44.6 MB (3.4 + 41.2) | **66 KB** | 122 MB (81 + 41) |

## Findings

1. **#3 external (arch A) — the only true delta.** Dolt versions logically, so each
   snapshot adds a flat **~66 KB** regardless of data size, at ~40 ms. Best for
   branching big data often. The moment Dolt lives *inside* the checkpoint (any of the
   #1/#2 variants) you capture the whole thing and lose this.

2. **#1 fs-only — cheapest checkpoint, whole-repo storage.** ~12 ms (pure OverlayFS op,
   flat with size; no DB process), but stores ~the whole repo per snapshot (a dolt
   commit rewrites enough storage that OverlayFS copies it up). Like arch B, but Dolt's
   format is ~4× more compact than SQLite (17.6 MB vs 76 MB at 1M).

3. **Fair control — #1 in build mode isolates the cost of build mode vs the DB server.**
   With #1 and #2 now on identical build-mode CRIU machinery (same sandbox, same
   `waypoint create` with memory), the only difference is the running server:
   - **#1 fs-only → #1 build/CRIU** = the cost of *build mode itself*: ~12 → ~138 ms,
     and a ~3 MB CRIU dump of an **idle shell** (flat — no data in RAM). Build mode adds
     a fixed CRIU tax even when there's nothing useful to capture.
   - **#1 build/CRIU → #2** = the cost of the **running dolt server**: identical repo fs
     (41 MB at 1M), but CRIU memory jumps from ~3 MB (idle shell) to **35→81 MB** (server
     buffer pool/sessions, growing with data), and latency +~93–183 ms. So #2's entire
     premium is dumping the server's RAM.

4. **#2 is the only one that preserves a warm DB**, at the highest cost (~120 MB/snapshot
   at 1M) and the most tool work: it required `GODEBUG=multipathtcp=0` (Go MPTCP breaks
   CRIU), dolt telemetry off (no external `:443` at checkpoint), and a Waypoint
   `criu --file-locks` patch (dolt holds DB file locks).

## Picking a structure

- Branch a large dataset often, cheap storage → **#3 external (arch A)**.
- Simplest, fastest checkpoint, don't mind whole-DB storage, no need for warm DB →
  **#1 fs-only**.
- Need the DB's **warm in-memory state** captured atomically with the app → **#2**, and
  pay the latency, storage, and tool-patch cost. (#1 build/CRIU exists mainly as the
  control that shows how much of that cost is the server vs build mode itself.)

## Reproduce

```bash
sudo env PATH=$PWD/.venv/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  DEMO_STATEFORK_ROOT=/path/to/Andy_StateFork \
  .venv/bin/python scripts/bench-three-structures.py 1000 100000 1000000
```
#2 and #1-build require a `waypoint` built with `criu --file-locks`.
