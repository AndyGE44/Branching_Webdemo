# Placements of a Dolt data tier — benchmark (2026-06-23)

Same data engine (**Dolt**), different placements relative to the StateFork checkpoint,
each measured in **both process (fs-only) and build (CRIU) mode** where applicable, so
the structures can be compared under identical checkpoint machinery.

| | placement / mode | versioning | checkpoint captures |
|---|---|---|---|
| **#1 fs-only** | repo files in checkpoint, process mode | StateFork fs | repo files |
| **#1 build/CRIU** | repo files in checkpoint, build mode, no server | StateFork CRIU+fs | repo + idle-shell memory |
| **#3 fs-only** | `dolt sql-server` external, process-mode app | Dolt branches | (tiny app fs) |
| **#3 build/CRIU** | external server, **build-mode app (CRIU)** | Dolt branches | app memory (idle shell), data stays external |
| **#2 full-system** | `dolt sql-server` **and an app client** inside the sandbox | StateFork CRIU+fs | repo + **DB-server** memory + **app + its live connection** |

Harness: `scripts/bench-three-structures.py`. Each snapshot follows a 200-row UPDATE.
Medians of k=3 after a warmup.

**#2 is now a faithful full-system checkpoint**: the app client lives *inside* the sandbox
alongside the server and holds one warm connection over the loopback. CRIU captures the
server, the app, and the established TCP connection between them (waypoint passes
`criu --tcp-established --file-locks`), so a restore needs **no reconnect** — asserted below.
The data-dirtying 200-row UPDATE is still host-driven and closed before each snapshot, so the
per-snapshot delta stays identical to the other structures; the only added cost vs a
server-only #2 is the app process + its live socket in the image.

**Overview diagram:** [`three-structures-overview.svg`](three-structures-overview.svg) shows, for
each placement, how it connects to the DB, how snapshot/restore behave, and how per-snapshot
storage splits between RAM (CRIU image) and disk.

## Snapshot latency (ms)

| rows | #1 fs | #1 build | #3 fs | #3 build | #2 |
|-----:|----:|----:|----:|----:|----:|
| 1k   | 12.3 | 140.4 | 38.4 | 161.2 | 339.0 |
| 100k | 13.1 | 137.5 | 39.9 | 157.3 | 340.2 |
| 1M   | 14.4 | 135.3 | 40.0 | 160.4 | 441.3 |

## Restore latency (ms)

| rows | #1 fs | #1 build | #3 fs | #3 build | #2 |
|-----:|----:|----:|----:|----:|----:|
| 1k   | 13.9 | 189.9 | 34.4 | 211.4 | 343.1 |
| 100k | 17.9 | 188.1 | 34.7 | 214.0 | 339.5 |
| 1M   | 14.6 | 188.4 | 35.2 | 214.3 | 379.6 |

## Per-snapshot storage (MB)

| rows | #1 fs | #1 build | #3 fs | #3 build | #2 |
|-----:|----:|----:|----:|----:|----:|
| 1k   | 0.03 | 3.0 | 0.04 | 2.3 | 46.5 |
| 100k | 1.73 | 6.8 | 0.05 | 2.4 | 51.6 |
| 1M   | 17.6 | 44.7 | 0.07 | 2.3 | 134.9 |

(#1/#2 = criu memory + fs; #2's criu now includes the server RAM **and** the app client.
#3 fs = Dolt delta only; #3 build = idle-shell criu + external delta.)

## Connection survival across restore (#2 only)

The in-sandbox app opens **one** connection to the co-located server and heartbeats on it.
Across a snapshot → restore, the heartbeat keeps advancing on the **same** server-side
session id, proving the exact connection was checkpointed and restored on both ends — the
app resumes mid-connection with no reconnect:

| rows | server CONNECTION_ID before → after restore | heartbeat before → after | reconnect? |
|-----:|:--:|:--:|:--:|
| 1k   | 2 → 2 | 4 → 9 | none |
| 100k | 2 → 2 | 4 → 9 | none |
| 1M   | 2 → 2 | 5 → 10 | none |

This is the property the external structures structurally **cannot** offer: in #1/#3 the data
tier lives outside the checkpoint, so after a restore the app must reconnect (and re-warm).
It is also exactly why #2 is the heaviest — you pay to freeze the app + its live socket, and
in return you skip the reconnect/warmup.

## Point-write throughput (data tier)

Measured **per placement** (no shared/borrowed number): #1 via the dolt CLI, #2 against the
server **inside the sandbox** (timed by an in-sandbox client), #3 against the **external** host
server. All are PK point writes; #2/#3 reuse one pooled connection.

| #1 dolt CLI (process/query) | #2 server (in sandbox) | #3 server (external, pooled) |
|----:|----:|----:|
| 22 ops/s | 1,452 ops/s | 1,435 ops/s |

#2 ≈ #3 (within noise) — running the server *inside* the CRIU/Waypoint sandbox costs nothing for
steady-state point-writes. The ~65× gap is the **access method** (server + pooled vs a `dolt`
process per query), not the placement. Connect-per-query vs pooled adds another ~35×; see
`benchmark-pool`.

## Findings

1. **Build mode levies a flat CRIU tax, independent of where the data is.** fs-only → build:
   snapshot 12 → 140 ms (#1) and 38 → 161 ms (#3); restore 14 → 190 ms (#1) and 34 → 211 ms
   (#3). The extra is CRIU dumping a ~2–3 MB **idle shell** — there's no useful memory to
   capture, you just pay for build mode.

2. **Among build-mode structures, keeping data external (#3-build) gives the smallest,
   flattest checkpoint.** Per-snapshot storage: **#3-build ~2.3 MB flat** (idle-shell memory +
   a negligible external Dolt delta) vs **#1-build → 44.7 MB** (repo files in the fs) vs
   **#2 → 135 MB** (server + app memory + repo) at 1M. Same latency tier, very different
   storage — because #3's data never enters the checkpoint.

3. **#2 is the only one that captures the warm DB *and* the live app/connection**, and it's the
   heaviest (snapshot grows 339 → 441 ms; storage to ~135 MB at 1M) because it dumps the
   server's RAM plus the app client — the CRIU image is 46 → 94 MB. It also needed the most
   tooling: `GODEBUG=multipathtcp=0`, dolt telemetry off, and `criu --file-locks
   --tcp-established` (both already passed by waypoint).

4. **#3 fs-only (arch A) remains the storage champion** at ~36–66 KB/snapshot flat (pure Dolt
   delta, no CRIU). The build-mode app checkpoint only adds value if the app itself has
   in-memory state worth preserving.

5. **Throughput is about access method, not placement — now measured per placement, not
   assumed.** Each was timed separately: #1 dolt CLI ~22 ops/s, #2 in-sandbox server ~1,452
   ops/s, #3 external server ~1,435 ops/s. #2 ≈ #3 confirms the CRIU/Waypoint sandbox adds no
   steady-state cost; the ~65× gap is server-vs-CLI (a process per query), independent of where
   the data sits.

6. **Only #2 keeps a live connection across a restore (no reconnect).** Because the app and the
   server are in the same checkpoint, the established TCP session survives `criu
   --tcp-established` — the app resumes on the same `CONNECTION_ID` (see the table above). #1/#3
   restore the app alone, so their clients must reconnect to the (external/file) data tier.

## Picking a structure

- Branch big data often, minimal checkpoint, fast queries → **#3 (external Dolt + server)**;
  add build-mode only if the app has memory state worth keeping (#3-build).
- Simplest, fastest checkpoint, small data → **#1 fs-only**.
- Must capture the DB's **warm in-memory state atomically with the app** *and* keep the app's
  live connection/session across a restore (no reconnect, no warmup) → **#2**, paying the
  latency/storage/tooling cost (~135 MB and ~440 ms per snapshot at 1M).

## Reproduce

```bash
sudo env PATH=$PWD/.venv/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  DEMO_STATEFORK_ROOT=/path/to/Andy_StateFork \
  .venv/bin/python scripts/bench-three-structures.py 1000 100000 1000000
```
#1-build / #2 / #3-build require a `waypoint` built with `criu --file-locks --tcp-established`
(both are already passed by the bundled waypoint). #2 also installs `pymysql` into the sandbox
image for its in-sandbox app client.
