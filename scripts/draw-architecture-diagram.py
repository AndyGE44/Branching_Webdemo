#!/usr/bin/env python3
"""Generate the build-mode architecture diagram as a vector SVG (no deps).

Run:  python scripts/draw-architecture-diagram.py
Out:  docs/architecture-build-mode.svg
"""
from __future__ import annotations
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "docs" / "architecture-build-mode.svg"

INK = "#1f2933"      # primary text
MUTED = "#5f6b7a"    # secondary text
BLUE_F, BLUE_S = "#e8f1fb", "#2f6fb0"   # control plane / app
TEAL_F, TEAL_S = "#e1f5ee", "#1d9e75"   # arch B data (in checkpoint)
CORAL_F, CORAL_S = "#faece7", "#c0532a" # arch A external Dolt
GRAY_F, GRAY_S = "#f3f4f6", "#9aa3ad"   # semantics panel
SAND = "#8a8a8a"     # sandbox dashed border
FONT = "Helvetica, Arial, sans-serif"

p: list[str] = []

def box(x, y, w, h, fill, stroke, rx=8, dash=False, sw=1.5):
    d = ' stroke-dasharray="6 5"' if dash else ""
    p.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
             f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>')

def txt(x, y, s, size=15, weight=400, anchor="middle", fill=INK):
    p.append(f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" '
             f'font-weight="{weight}" text-anchor="{anchor}" fill="{fill}">{s}</text>')

def arrow(x1, y1, x2, y2, color=MUTED):
    p.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
             f'stroke-width="1.6" marker-end="url(#ah)"/>')

W, H = 1040, 700
p.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">')
p.append('<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
         'markerHeight="7" orient="auto-start-reverse">'
         f'<path d="M0,0 L10,5 L0,10 z" fill="{MUTED}"/></marker></defs>')
box(0, 0, W, H, "#ffffff", "#ffffff", rx=0, sw=0)

txt(W / 2, 34, "Branching_Webdemo - build-mode architecture", 20, 600)
txt(W / 2, 58, "StateFork checkpoints the app tier (Waypoint: CRIU memory + filesystem); "
               "Dolt is the data tier", 13, 400, fill=MUTED)

box(445, 80, 150, 40, BLUE_F, BLUE_S)
txt(520, 105, "Browser / API client", 14)
arrow(520, 120, 520, 150)

box(370, 150, 300, 58, BLUE_F, BLUE_S)
txt(520, 175, "Control Plane - FastAPI :8000 (host, sudo)", 14, 500)
txt(520, 195, "drives StateFork / Waypoint; versions Dolt for arch A", 12, 400, fill=MUTED)
arrow(465, 208, 285, 256)
arrow(575, 208, 760, 256)

txt(270, 248, "Arch B - data inside the checkpoint", 15, 500, fill=TEAL_S)
box(70, 262, 400, 196, "#fbfbfa", SAND, dash=True)
txt(270, 285, "Waypoint sandbox  (fs isolated via OverlayFS; network shared)", 11.5, 400, fill=MUTED)
box(110, 300, 320, 56, BLUE_F, BLUE_S)
txt(270, 323, "inventory app (uvicorn)", 14)
txt(270, 343, "+ RSS = process memory (CRIU dumps this)", 11.5, 400, fill=MUTED)
box(110, 376, 320, 48, TEAL_F, TEAL_S)
txt(270, 400, "SQLite  /demo_inventory.db", 13.5)
txt(270, 417, "(lives in the checkpoint)", 11, 400, fill=TEAL_S)
txt(270, 482, "checkpoint = app memory + the DB file", 13, 400, fill=INK)

txt(770, 248, "Arch A - external Dolt data tier", 15, 500, fill=CORAL_S)
box(570, 262, 400, 130, "#fbfbfa", SAND, dash=True)
txt(770, 285, "Waypoint sandbox  (fs isolated; network shared)", 11.5, 400, fill=MUTED)
box(610, 300, 320, 56, BLUE_F, BLUE_S)
txt(770, 323, "inventory app (uvicorn)  + PyMySQL", 14)
txt(770, 343, "+ RSS = process memory (CRIU dumps this)", 11.5, 400, fill=MUTED)
box(610, 410, 320, 50, CORAL_F, CORAL_S)
txt(770, 431, "Dolt sql-server (host, OUTSIDE sandbox)", 12.5)
txt(770, 449, "versioned by CALL DOLT_COMMIT / BRANCH / RESET", 10.5, 400, fill=CORAL_S)
arrow(770, 356, 770, 410, CORAL_S)
txt(905, 388, "TCP :3306", 11, 400, anchor="end", fill=MUTED)
txt(770, 482, "checkpoint = app memory + tiny fs (data is external)", 13, 400, fill=INK)

box(70, 506, 900, 158, GRAY_F, GRAY_S)
txt(92, 534, "Snapshot / restore (paired across both tiers)", 14, 500, anchor="start")
txt(92, 562, "snapshot(id) = waypoint create : CRIU memory + OverlayFS upper    "
             "[arch A also: dolt commit + branch sf_ID]", 12.5, 400, anchor="start")
txt(92, 586, "restore(id)  = waypoint restore : CRIU memory + fs, SAME PID     "
             "[arch A also: dolt reset --hard sf_ID]", 12.5, 400, anchor="start")
txt(92, 620, "Process mode (alternative): app runs on the host; Waypoint stores the filesystem only "
             "(no memory capture).", 11.5, 400, anchor="start", fill=MUTED)
txt(92, 642, "Build mode (shown) captures memory, so snapshot/restore cost is dominated by RSS - "
             "see docs/benchmark-build-mode.md", 11.5, 400, anchor="start", fill=MUTED)

p.append("</svg>")
OUT.write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + "\n".join(p) + "\n", encoding="utf-8")
print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
