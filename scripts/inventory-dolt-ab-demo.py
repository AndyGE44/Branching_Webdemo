#!/usr/bin/env python3
"""End-to-end demo of architecture A on the inventory app.

Architecture A = StateFork checkpoints only the app tier, while an *external*
Dolt database is the data tier, versioned by StateFork's ``DoltController`` using
Dolt's own branching. This script proves the data tier follows StateFork
snapshots/restores without the DB ever living inside the checkpoint:

    init seed -> snapshot(base) -> agent edits -> snapshot(v1)
              -> restore(base)  (data rolls back)
              -> restore(v1)    (data rolls forward)

Requirements:
- ``dolt`` on PATH (https://github.com/dolthub/dolt).
- StateFork checked out; point DEMO_STATEFORK_ROOT at it (default below).

Run:
    DEMO_STATEFORK_ROOT=/users/alexxjk/Andy_StateFork \
        python scripts/inventory-dolt-ab-demo.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
sys.path.insert(0, str(SRC))

STATEFORK_ROOT = Path(
    os.getenv("DEMO_STATEFORK_ROOT", "/users/alexxjk/Andy_StateFork")
)


def main() -> int:
    if shutil.which("dolt") is None:
        print("ERROR: `dolt` not found on PATH. Install from "
              "https://github.com/dolthub/dolt", file=sys.stderr)
        return 2
    if not STATEFORK_ROOT.exists():
        print(f"ERROR: StateFork not found at {STATEFORK_ROOT}. "
              f"Set DEMO_STATEFORK_ROOT.", file=sys.stderr)
        return 2
    sys.path.insert(0, str(STATEFORK_ROOT))

    from agent_safe_demo.app_plane.inventory_service.store import DoltInventoryStore
    from controller import DoltController

    workdir = Path(tempfile.mkdtemp(prefix="inventory_dolt_ab_"))
    repo = workdir / "inventory_dolt"
    try:
        # Data tier: external Dolt repo the app talks to directly.
        store = DoltInventoryStore(repo)
        store.init()
        print(f"[init] external Dolt data tier at {repo}")
        print(f"       seed summary: {store.state()['summary']}")

        # StateFork versions that external DB via Dolt branching.
        dolt = DoltController(repo_dir=str(repo))
        if not dolt.enabled:
            print("ERROR: DoltController disabled (dolt missing?)", file=sys.stderr)
            return 2

        dolt.snapshot("base")
        print("[snapshot base] committed seed, branch sf_base")

        store.reserve("MCU-100", 2, "agent")
        store.sell("SENSOR-9", 1, "agent")
        print(f"[agent edits] summary: {store.state()['summary']}")
        dolt.snapshot("v1")
        print("[snapshot v1] committed agent edits, branch sf_v1")

        dolt.restore("base")
        rolled = store.state()["summary"]
        mcu = store.inventory_item("MCU-100")
        print(f"[restore base] data rolled back: {rolled} | "
              f"MCU-100 available={mcu['available']} reserved={mcu['reserved']}")
        assert rolled["reservations"] == 0 and mcu["available"] == 8

        dolt.restore("v1")
        fwd = store.state()["summary"]
        print(f"[restore v1] data rolled forward: {fwd}")
        assert fwd["reservations"] == 1

        dolt.cleanup()
        print("\nOK: architecture A verified end-to-end on the inventory app.")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
