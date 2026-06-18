# Architecture A vs B — benchmark (2026-06-17)

Component-level benchmark on the CRIU node (CloudLab, Ubuntu 24.04, kernel 6.8;
CRIU 4.2, Waypoint v0.6.0, dolt 2.1.8). Uses the **real** classes the control
plane uses, isolating the data-size variable:

- **B** — data is a SQLite file inside the Waypoint work_dir; `WaypointBuildManager(build=False).snapshot()/restore()` (OverlayFS+CRIU, fs-only in process mode).
- **A-cli** — external Dolt repo via `DoltController` (`add/commit/branch -f`; `checkout`+`reset --hard`).
- **A-server** — external Dolt via a long-lived `dolt sql-server`, `DoltServerDataTier.on_snapshot()/on_restore()` (`CALL DOLT_*`).

Table `parts(id,sku,name,qty,price,location,updated_at)`; each snapshot follows a
200-row UPDATE (a realistic small delta). Latencies are medians of k=7 (≤100k) /
k=4 (1M) runs after a warmup. App-tier Waypoint constant (empty work_dir):
**snapshot 14.6 ms / restore 14.4 ms** — arch A pays this *on top of* its Dolt op.

## Snapshot / restore latency (median ms)

| rows | B snap | B restore | A-srv snap¹ | A-srv restore¹ | A-cli snap¹ | A-cli restore¹ |
|-----:|------:|------:|------:|------:|------:|------:|
| 1 k   | 14.2 | 14.7 | 24.3 | 21.4 | 144 | 139 |
| 100 k | 15.7 | 14.9 | 24.8 | 21.7 | 148 | 156 |
| 1 M   | 15.8 | 14.0 | 25.7 | 20.3 | 161 | 163 |

¹ Dolt-op only. End-to-end arch A adds the +14.6/+14.4 ms app-tier Waypoint op:
A-server ≈ **39 / 35 ms**, A-cli ≈ **176 / 177 ms**.

**All three are flat across size.** The expected "B grows with data size" did **not**
happen: Waypoint snapshot/restore is OverlayFS layer manipulation, not a file copy
(the copy-up happens at *write* time, not at snapshot time), so B's checkpoint stays
~15 ms even for a 76 MB DB. **B is the latency winner at every size.**

## Storage

| rows | B per-snapshot (full file²) | A per-snapshot (delta) | A repo total | B SQLite file |
|-----:|------:|------:|------:|------:|
| 1 k   | 80 KB  | 130 KB | 0.19 MB | 80 KB  |
| 100 k | 7.4 MB | 162 KB | 3.5 MB  | 7.4 MB |
| 1 M   | 76 MB  | 99 KB  | 33.5 MB | 76 MB  |

² OverlayFS copies the whole single-file DB up on any write, so each B snapshot that
follows a write stores ~the full file. Dolt is content-addressed → each snapshot
stores only the changed chunks (~flat, delta-bound), and the base repo is ~2.3×
smaller than the SQLite file (33.5 MB vs 76 MB at 1 M).

(`store_total_bytes` from `du` on the live overlay mount returned a bogus value and
is omitted; `snap_upper_bytes`/`dolt_bytes` are reliable.)

## Steady-state point-write throughput @100k (updates/sec)

| SQLite (B) | dolt-server (A) | dolt-cli (A) |
|------:|------:|------:|
| 6 524 | 1 210 | 20.3 |

Embedded SQLite ≈ **5×** dolt-server; the CLI backend (process per query) is **~320×**
slower and unusable under load — only `dolt_server` is viable for throughput.

## The crossover

There is **no latency crossover** — B wins latency and throughput at all sizes.
The real crossover is **storage per snapshot**:

- B ≈ 76 bytes/row × rows (full file each snapshot); A ≈ ~130 KB flat (200-row delta).
- Crossover ≈ **1.7 k rows**: below it B stores less, above it A wins and the gap grows
  linearly — **~46× at 100 k, ~765× at 1 M**. Smaller deltas push the crossover even lower.
- With S snapshots the gap compounds: B ≈ S × full_file, A ≈ base + S × delta.

## Takeaway

Arch A's win is **not** speed — it's **storage scalability across many snapshots/branches**
plus native branch/diff/merge semantics SQLite-in-checkpoint can't offer. Arch B is
faster (latency *and* throughput) and simpler, but every snapshot costs a full DB copy,
so storage explodes with size × snapshot count. Pick A when you branch a large dataset
often; pick B for small data or write-heavy steady state with few snapshots. For A,
always use `dolt_server` (the CLI's per-query spawn kills both snapshot latency and
throughput).
