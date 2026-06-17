#!/usr/bin/env python3
"""End-to-end demo of architecture A over a long-lived ``dolt sql-server`` (C).

Same architecture-A flow as ``inventory-dolt-ab-demo.py`` (the CLI version), but
the data tier is a persistent MySQL-protocol server instead of per-query
``dolt`` process spawns:

- the app store talks to the server with PyMySQL + bind parameters, and
- snapshot/restore are server-native (``CALL DOLT_ADD/COMMIT/BRANCH/RESET``),
  driven by the control-plane data tier (no CLI contention with the server).

    start server -> seed -> on_snapshot(base) -> edits -> on_snapshot(v1)
                 -> on_restore(base)  (data rolls back)
                 -> on_restore(v1)    (data rolls forward)

Requirements: ``dolt`` on PATH and ``PyMySQL`` importable (``pip install -e .``).

Run:
    python scripts/inventory-dolt-server-demo.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

PORT = int(__import__("os").getenv("DEMO_INVENTORY_DOLT_PORT", "3306"))


def main() -> int:
    if shutil.which("dolt") is None:
        print("ERROR: `dolt` not found on PATH.", file=sys.stderr)
        return 2
    try:
        import pymysql  # noqa: F401
    except ModuleNotFoundError:
        print("ERROR: PyMySQL not installed. Run `pip install -e .`.", file=sys.stderr)
        return 2

    from agent_safe_demo.control_plane.dolt_server import DoltSqlServer
    from agent_safe_demo.control_plane.data_tier import DoltServerDataTier
    from agent_safe_demo.app_plane.inventory_service.store import DoltServerInventoryStore

    work = Path(tempfile.mkdtemp(prefix="inventory_dolt_server_"))
    repo = work / "inventory"
    server = DoltSqlServer(repo, port=PORT)
    try:
        server.start()
        db = server.database
        print(f"[server] dolt sql-server up on 127.0.0.1:{PORT}, database={db}")

        store = DoltServerInventoryStore(host="127.0.0.1", port=PORT, database=db)
        tier = DoltServerDataTier(host="127.0.0.1", port=PORT, database=db)

        store.init()
        print(f"[init] seeded via server: {store.state()['summary']}")
        tier.on_snapshot("base")
        print("[snapshot base] CALL DOLT_COMMIT + DOLT_BRANCH sf_base")

        store.reserve("MCU-100", 2, "agent")
        store.sell("SENSOR-9", 1, "agent")
        print(f"[agent edits] {store.state()['summary']}")
        tier.on_snapshot("v1")

        tier.on_restore("base")
        rolled = store.state()["summary"]
        mcu = store.inventory_item("MCU-100")
        print(f"[restore base] rolled back: {rolled} | MCU-100 available={mcu['available']}")
        assert rolled["reservations"] == 0 and mcu["available"] == 8

        tier.on_restore("v1")
        fwd = store.state()["summary"]
        print(f"[restore v1] rolled forward: {fwd}")
        assert fwd["reservations"] == 1

        tier.cleanup()
        print("\nOK: architecture A over a long-lived dolt sql-server verified.")
        return 0
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
