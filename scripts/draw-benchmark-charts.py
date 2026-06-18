#!/usr/bin/env python3
"""Render the A-vs-B benchmark results to vector SVG figures (matplotlib).

Reads the committed results JSON and writes:
  docs/benchmark-arch-a-vs-b.svg   (process mode: fs-only)
  docs/benchmark-build-mode.svg    (build mode: CRIU memory + fs)

Run:  python scripts/draw-benchmark-charts.py   (needs matplotlib)
"""
from __future__ import annotations
import json
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

DOCS = Path(__file__).resolve().parents[1] / "docs"
BLUE, TEAL, CORAL, AMBER = "#2f6fb0", "#1d9e75", "#d8572f", "#b5651d"
plt.rcParams.update({"font.size": 10, "axes.titlesize": 11, "axes.grid": True,
                     "grid.alpha": 0.25, "figure.autolayout": True})

def kfmt(v):  # bytes -> KB
    return v / 1024.0

# ---------------- process mode ---------------- #
proc = json.loads((DOCS / "benchmark-arch-a-vs-b.results.json").read_text())
const = proc["app_tier_const"]["snapshot_ms_med"]
sizes = sorted(proc["sizes"], key=int)
labels = ["1k", "100k", "1M"][: len(sizes)]
B_snap = [proc["sizes"][s]["B"]["snapshot_ms_med"] for s in sizes]
As_snap = [proc["sizes"][s]["A_server"]["snapshot_ms_med"] + const for s in sizes]
Ac_snap = [proc["sizes"][s]["A_cli"]["snapshot_ms_med"] + const for s in sizes]
B_store = [kfmt(proc["sizes"][s]["B"]["snap_upper_bytes"]) for s in sizes]
A_store = [kfmt(proc["sizes"][s]["A_cli"]["snap_delta_bytes"]) for s in sizes]
tp = proc["sizes"]["100000"]["throughput"]

fig, ax = plt.subplots(1, 3, figsize=(15, 4.3))
fig.suptitle("Architecture A (external Dolt) vs B (SQLite-in-checkpoint) - process mode (filesystem-only)",
             fontsize=13, weight="bold")
ax[0].plot(labels, B_snap, "-o", color=BLUE, label="B (SQLite-in-Waypoint)")
ax[0].plot(labels, As_snap, "--s", color=TEAL, label="A (dolt-server)")
ax[0].plot(labels, Ac_snap, ":^", color=CORAL, label="A (dolt-cli)")
ax[0].set_title("Snapshot latency vs data size (end-to-end)")
ax[0].set_xlabel("rows"); ax[0].set_ylabel("milliseconds"); ax[0].set_ylim(bottom=0); ax[0].legend()
ax[1].plot(labels, B_store, "-o", color=BLUE, label="B (full DB / snapshot)")
ax[1].plot(labels, A_store, "--s", color=TEAL, label="A (Dolt delta / snapshot)")
ax[1].set_yscale("log"); ax[1].set_title("Storage per snapshot vs data size")
ax[1].set_xlabel("rows"); ax[1].set_ylabel("KB per snapshot (log)"); ax[1].legend()
tps = [("SQLite (B)", tp["sqlite_ops_s"], BLUE), ("dolt-server (A)", tp["dolt_server_ops_s"], TEAL),
       ("dolt-cli (A)", tp["dolt_cli_ops_s"], CORAL)]
ax[2].bar([t[0] for t in tps], [t[1] for t in tps], color=[t[2] for t in tps], width=0.6)
ax[2].set_yscale("log"); ax[2].set_title("Point-write throughput @100k rows")
ax[2].set_ylabel("updates / sec (log)")
for i, t in enumerate(tps):
    ax[2].text(i, t[1] * 1.1, f"{t[1]:,.0f}", ha="center", va="bottom", fontsize=9)
fig.savefig(DOCS / "benchmark-arch-a-vs-b.svg")
plt.close(fig)

# ---------------- build mode ---------------- #
bm = json.loads((DOCS / "benchmark-build-mode.results.json").read_text())
rss = [bm["rss50_d0"], bm["rss200_d0"], bm["rss800_d0"]]
rss_x = [f"{r['rss_mb']} MB" for r in rss]
rss_snap = [r["snapshot_ms_med"] for r in rss]
rss_rest = [r["restore_ms_med"] for r in rss]
cats = ["B · 0", "B · 100k", "B · 1M", "A · 1M"]
src = [bm["rss200_d0"], bm["rss200_100k"], bm["rss200_1M"], bm["rss200_d0"]]
mem = [s["criu_mem_bytes"] / 1e6 for s in src]
data = [bm["rss200_d0"]["fs_upper_bytes"] / 1e6, bm["rss200_100k"]["fs_upper_bytes"] / 1e6,
        bm["rss200_1M"]["fs_upper_bytes"] / 1e6, 0.1]
b_build = [bm["rss200_d0"]["snapshot_ms_med"], bm["rss200_100k"]["snapshot_ms_med"],
           bm["rss200_1M"]["snapshot_ms_med"]]

fig, ax = plt.subplots(1, 3, figsize=(15, 4.3))
fig.suptitle("Build mode (CRIU memory + filesystem): the memory dump dominates",
             fontsize=13, weight="bold")
ax[0].plot(rss_x, rss_snap, "-o", color=CORAL, label="snapshot")
ax[0].plot(rss_x, rss_rest, "--s", color=TEAL, label="restore")
ax[0].set_title("Latency vs app RSS (the memory tax)")
ax[0].set_xlabel("resident memory (RSS)"); ax[0].set_ylabel("milliseconds"); ax[0].set_ylim(bottom=0); ax[0].legend()
ax[1].bar(cats, mem, color=BLUE, label="CRIU memory image")
ax[1].bar(cats, data, bottom=mem, color=AMBER, label="data (fs DB / dolt delta)")
ax[1].set_title("Per-snapshot storage @200 MB RSS")
ax[1].set_ylabel("MB per snapshot"); ax[1].legend()
ax[2].plot(labels, B_snap, "-o", color=TEAL, label="process mode (fs-only)")
ax[2].plot(labels, b_build, ":^", color=CORAL, label="build mode (memory, 200 MB RSS)")
ax[2].set_yscale("log"); ax[2].set_title("Cost of capturing memory: B snapshot")
ax[2].set_xlabel("rows"); ax[2].set_ylabel("ms (log)"); ax[2].legend()
fig.savefig(DOCS / "benchmark-build-mode.svg")
plt.close(fig)

print("wrote", DOCS / "benchmark-arch-a-vs-b.svg", "and", DOCS / "benchmark-build-mode.svg")
