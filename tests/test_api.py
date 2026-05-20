import importlib
import sys

from fastapi.testclient import TestClient


def load_app(monkeypatch, tmp_path):
    db_path = tmp_path / "toy_inventory.db"
    monkeypatch.setenv("TOY_INVENTORY_DB_PATH", str(db_path))
    monkeypatch.setenv("TOY_BRANCH_BACKEND", "local-copy")
    sys.modules.pop("agent_safe_demo.main", None)
    module = importlib.import_module("agent_safe_demo.main")
    return module.app


def test_inventory_seed_data(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/inventory")

    assert response.status_code == 200
    items = response.json()["items"]
    assert {item["id"] for item in items} >= {"MCU-100", "SENSOR-9", "MCU-ALT"}


def test_agent_demo_workflow_mutates_state(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        order = client.post(
            "/api/build-orders",
            json={
                "sku": "TEST-KIT",
                "part_id": "SENSOR-9",
                "quantity": 5,
                "actor": "agent",
            },
        )
        assert order.status_code == 200
        assert order.json()["status"] == "blocked"

        substitute = client.post(
            f"/api/build-orders/{order.json()['build_order_id']}/try-substitute",
            json={"substitute_part_id": "MCU-ALT", "actor": "agent"},
        )
        assert substitute.status_code == 200

        purchase_order = client.post(
            "/api/purchase-orders",
            json={"part_id": "SENSOR-9", "quantity": 6, "actor": "agent"},
        )
        assert purchase_order.status_code == 200

        state = client.get("/api/state").json()

    assert len(state["build_orders"]) == 1
    assert len(state["purchase_orders"]) == 1
    assert len(state["audit_log"]) == 4
