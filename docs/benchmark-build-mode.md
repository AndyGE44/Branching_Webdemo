# Build-mode (memory-capturing) benchmark — A vs B (2026-06-17)

The process-mode benchmark ([benchmark-arch-a-vs-b.md](benchmark-arch-a-vs-b.md)) was
filesystem-only — Waypoint skipped the CRIU memory dump. This run captures **memory**
(the actual reason to use StateFork over plain Dolt branching) by running both
architectures in **build mode** (`checkpoint_exec`: app runs inside the Waypoint env,
`waypoint create` checkpoints the live process tree with CRIU).

## Enabling memory capture (the fix)

StateFork's `WaypointAttachManager` hardcoded the `-2` "no PID" sentinel on
`waypoint create`, which forces a fs-only checkpoint — so even `checkpoint_exec` never
captured memory. Fix (`controller/waypoint_env_manager.py`): in build mode, omit the PID
so Waypoint auto-checkpoints the long-running shell session **with** memory (v0.5.0+).
Validated: a 200 MB-resident process snapshots to a ~216 MB CRIU image and restores into
the **same PID** (true memory restore, not a restart) — confirmed end-to-end through the
control-plane UI for both B (sqlite in env) and A (dolt sql-server over TCP from inside
the env; needs PyMySQL in the Dockerfile).

## Results

Latencies are medians of k=4; "CRIU mem" / "fs" are the per-checkpoint `criu` / `upper`
dir sizes. App RSS is set with an env-gated ballast in the inventory app.

### Memory tax (RSS sweep, no DB in env = arch A app-tier at each RSS)

| RSS | snapshot | restore | CRIU mem |
|----:|---------:|--------:|---------:|
| 50 MB  | 245 ms  | 217 ms | 62 MB  |
| 200 MB | 499 ms  | 269 ms | 217 MB |
| 800 MB | 1554 ms | 462 ms | 847 MB |

Snapshot ≈ ~150 ms fixed + ~1.7 ms/MB (~0.55 GB/s dump); restore scales sub-linearly.
CRIU image ≈ RSS + ~12 MB runtime.

### Data sweep (arch B: DB inside the env, RSS = 200 MB)

| rows | snapshot | restore | CRIU mem | fs (DB) |
|-----:|---------:|--------:|---------:|--------:|
| ~0   | 499 ms | 269 ms | 217 MB | ~0     |
| 100k | 506 ms | 270 ms | 218 MB | 5.7 MB |
| 1M   | 514 ms | 268 ms | 218 MB | 58.4 MB |

Arch A's data is external (dolt server), so its fs upper is ~0 at every size; its data
tier adds a flat ~25 ms commit / ~20 ms reset (from the process-mode run) and ~0.1 MB
delta storage.

## Findings

1. **The CRIU memory dump dominates.** Snapshot/restore latency tracks **RSS**, not data
   size. B's snapshot barely moves (499→514 ms) as its in-env DB grows to 58 MB — the
   OverlayFS fs capture is nearly free; the ~217 MB memory dump is the cost.
2. **A and B converge on latency.** Both pay the same memory tax (~500 ms @200 MB RSS).
   A is marginally *slower* (+~25 ms dolt commit) where B adds ~0 (fs is free in time).
   The process-mode latency gap (B ~15 ms vs A ~39 ms) collapses to noise once memory is
   captured.
3. **A's storage edge persists but shrinks in relative terms.** Per snapshot at 1M/200 MB:
   B ≈ 217 MB (mem) + 58 MB (full DB, re-stored every snapshot) = ~275 MB; A ≈ 217 MB
   (mem) + ~0.1 MB (dolt delta). A still avoids re-storing the dataset, but the memory
   image — paid by **both** — is now the bulk of every checkpoint.
4. **Memory capture is the expensive part.** B's 1M snapshot: 15 ms (process/fs-only) →
   514 ms (build, 200 MB RSS) — ~34× and growing with RSS.

## Takeaway

When you actually need the app's in-memory state (StateFork's whole point), the data-tier
choice (A vs B) is a secondary effect: both architectures are dominated by the cost of
checkpointing RAM. Arch A's advantages narrow to (a) not re-storing the dataset per
snapshot and (b) branch/diff/merge semantics; the primary optimization target becomes the
memory checkpoint itself (incremental / deduplicated CRIU, lower app RSS), not the data
tier. Process mode remains the right choice for a stateless app where the app-tier
checkpoint is redundant and arch A's data-tier storage win is the whole story.
