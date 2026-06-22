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
                     "grid.alpha": 0.25, "figure.autolayout": True,
                     "svg.hashsalt": "branching-webdemo-bench"})  # deterministic element ids

def kfmt(v):  # bytes -> KB
    return v / 1024.0

# ---------------- process mode ---------------- #
proc = json.loads((DOCS / "benchmark-arch-a-vs-b.results.json").read_text())
const = proc["app_tier_const"]["snapshot_ms_med"]
sizes = sorted(proc["sizes"], key=int)
labels = ["1k", "100k", "1M"][: len(sizes)]
B_snap = [proc["sizes"][s]["B"]["snapshot_ms_med"] for s in sizes]
As_snap = [proc["sizes"][s]["A_server"]["snapshot_ms_med"] + const for s in sizes]
B_store = [kfmt(proc["sizes"][s]["B"]["snap_upper_bytes"]) for s in sizes]
A_store = [kfmt(proc["sizes"][s]["A_cli"]["snap_delta_bytes"]) for s in sizes]
tp = proc["sizes"]["100000"]["throughput"]

fig, ax = plt.subplots(1, 3, figsize=(15, 4.3))
fig.suptitle("Architecture A (external Dolt) vs B (SQLite-in-checkpoint) - process mode (filesystem-only)",
             fontsize=13, weight="bold")
ax[0].plot(labels, B_snap, "-o", color=BLUE, label="B (SQLite-in-Waypoint)")
ax[0].plot(labels, As_snap, "--s", color=TEAL, label="A (external Dolt)")
ax[0].set_title("Snapshot latency vs data size (end-to-end)")
ax[0].set_xlabel("rows"); ax[0].set_ylabel("milliseconds"); ax[0].set_ylim(bottom=0); ax[0].legend()
ax[1].plot(labels, B_store, "-o", color=BLUE, label="B (full DB / snapshot)")
ax[1].plot(labels, A_store, "--s", color=TEAL, label="A (external Dolt, delta / snapshot)")
ax[1].set_yscale("log"); ax[1].set_title("Storage per snapshot vs data size")
ax[1].set_xlabel("rows"); ax[1].set_ylabel("KB per snapshot (log)"); ax[1].legend()
# Throughput: single point-UPDATE ops/s. B = in-process SQLite; A = external dolt-server,
# pooled (realistic) vs connect-per-query. A numbers come from the pool benchmark when present.
tps = [("SQLite (B)", tp["sqlite_ops_s"], BLUE)]
_pool_path = DOCS / "benchmark-pool.results.json"
if _pool_path.exists():
    _p = json.loads(_pool_path.read_text())
    tps += [("Dolt pooled (A)", _p["5"]["single_update_ops_s"], TEAL),
            ("Dolt per-query (A)", _p["0"]["single_update_ops_s"], CORAL)]
else:
    tps += [("Dolt server (A)", tp["dolt_server_ops_s"], TEAL)]
ax[2].bar([t[0] for t in tps], [t[1] for t in tps], color=[t[2] for t in tps], width=0.6)
ax[2].set_yscale("log"); ax[2].set_title("Point-write throughput (single UPDATE)")
ax[2].set_ylabel("updates / sec (log)"); ax[2].tick_params(axis="x", labelrotation=12)
for i, t in enumerate(tps):
    ax[2].text(i, t[1] * 1.13, f"{t[1]:,.0f}", ha="center", va="bottom", fontsize=8.5)
fig.savefig(DOCS / "benchmark-arch-a-vs-b.svg", metadata={"Date": None})
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
fig.savefig(DOCS / "benchmark-build-mode.svg", metadata={"Date": None})
plt.close(fig)

# ---------------- connection pool (arch A) ---------------- #
pool_path = DOCS / "benchmark-pool.results.json"
if pool_path.exists():
    pool = json.loads(pool_path.read_text())
    pq, pl = pool["0"], pool["5"]
    groups = ["single point-UPDATE", "full buy() request"]
    perq = [pq["single_update_ops_s"], pq["buy_request_ops_s"]]
    pooled = [pl["single_update_ops_s"], pl["buy_request_ops_s"]]
    x = list(range(len(groups))); w = 0.38
    fig, ax = plt.subplots(1, 1, figsize=(7.5, 4.3))
    ax.bar([i - w / 2 for i in x], perq, w, color=CORAL, label="connect per query")
    ax.bar([i + w / 2 for i in x], pooled, w, color=TEAL, label="pooled (realistic)")
    ax.set_yscale("log"); ax.set_xticks(x); ax.set_xticklabels(groups)
    ax.set_ylabel("ops / sec (log)"); ax.legend()
    ax.set_title("Arch A throughput: connection pool vs connect-per-query (dolt-server)")
    for i, v in enumerate(perq):
        ax.text(i - w / 2, v * 1.12, f"{v:.0f}", ha="center", fontsize=9)
    for i, v in enumerate(pooled):
        ax.text(i + w / 2, v * 1.12, f"{v:.0f}", ha="center", fontsize=9)
    fig.tight_layout(); fig.savefig(DOCS / "benchmark-pool.svg", metadata={"Date": None})
    plt.close(fig)

# ---------------- three placements of a Dolt data tier ---------------- #
three_path = DOCS / "benchmark-three-structures.results.json"
if three_path.exists():
    doc = json.loads(three_path.read_text())
    t3 = doc["sizes"]
    szs = sorted(t3, key=int)
    xl = ["1k", "100k", "1M"][: len(szs)]
    g = lambda key: [t3[s][key] for s in szs]
    mb = lambda b: b / 1e6
    store_fn = {  # per-snapshot total storage by structure
        "s1_coupled": lambda r: mb(r["fs_upper_bytes"]),
        "s1_build": lambda r: mb(r["criu_bytes"] + r["fs_upper_bytes"]),
        "s3_external": lambda r: mb(r["data_delta_bytes"]),
        "s3_build": lambda r: mb(r["criu_bytes"] + r["fs_upper_bytes"] + r.get("data_delta_bytes", 0)),
        "s2_fullsystem": lambda r: mb(r["criu_bytes"] + r["fs_upper_bytes"]),
    }
    # (label, key, color, style) -- color = structure family, dashed = build mode
    series = [("#1 coupled, fs-only", "s1_coupled", BLUE, "-o"),
              ("#1 coupled, build/CRIU", "s1_build", BLUE, "--o"),
              ("#3 external, fs-only", "s3_external", TEAL, "-s"),
              ("#3 external, build/CRIU", "s3_build", TEAL, "--s"),
              ("#2 full-system", "s2_fullsystem", CORAL, ":^")]
    fig, ax = plt.subplots(2, 2, figsize=(13, 9))
    fig.suptitle("Placements of a Dolt data tier across modes (snapshot after a 200-row change)",
                 fontsize=14, weight="bold")
    for lab, key, col, sty in series:
        ser = g(key)
        ax[0, 0].plot(xl, [r["snap_ms"] for r in ser], sty, color=col, label=lab)
        ax[0, 1].plot(xl, [r["rest_ms"] for r in ser], sty, color=col, label=lab)
        ax[1, 0].plot(xl, [store_fn[key](r) for r in ser], sty, color=col, label=lab)
    ax[0, 0].set_title("Snapshot latency"); ax[0, 0].set_ylabel("ms"); ax[0, 0].set_ylim(bottom=0); ax[0, 0].legend(fontsize=8)
    ax[0, 1].set_title("Restore latency"); ax[0, 1].set_ylabel("ms"); ax[0, 1].set_ylim(bottom=0); ax[0, 1].legend(fontsize=8)
    ax[1, 0].set_yscale("log"); ax[1, 0].set_title("Per-snapshot storage"); ax[1, 0].set_xlabel("rows")
    ax[1, 0].set_ylabel("MB per snapshot (log)"); ax[1, 0].legend(fontsize=8)
    tp = doc.get("throughput", {})
    if tp and "error" not in tp:
        bars = [("#1 dolt CLI", tp["dolt_cli_ops_s"], BLUE),
                ("#2 / #3 dolt server\n(pooled)", tp["dolt_server_pooled_ops_s"], TEAL)]
        ax[1, 1].bar([b[0] for b in bars], [b[1] for b in bars], color=[b[2] for b in bars], width=0.5)
        ax[1, 1].set_yscale("log"); ax[1, 1].set_title("Point-write throughput (data tier)")
        ax[1, 1].set_ylabel("ops / sec (log)")
        for i, b in enumerate(bars):
            ax[1, 1].text(i, b[1] * 1.15, f"{b[1]:,.0f}", ha="center", fontsize=9)
    else:
        ax[1, 1].axis("off")
    fig.tight_layout(); fig.savefig(DOCS / "benchmark-three-structures.svg", metadata={"Date": None})
    plt.close(fig)

print("wrote benchmark SVGs to", DOCS)
