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


def item_by_id(items, part_id):
    return next(item for item in items if item["id"] == part_id)


def test_inventory_actions_mutate_state(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        buy = client.post(
            "/api/inventory/buy",
            json={"part_id": "SENSOR-9", "quantity": 4, "actor": "user"},
        )
        assert buy.status_code == 200
        assert buy.json()["part"]["on_hand"] == 6

        sell = client.post(
            "/api/inventory/sell",
            json={"part_id": "SENSOR-9", "quantity": 3, "actor": "user"},
        )
        assert sell.status_code == 200
        assert sell.json()["part"]["on_hand"] == 3

        reserve = client.post(
            "/api/reservations",
            json={"part_id": "SENSOR-9", "quantity": 2, "actor": "user"},
        )
        assert reserve.status_code == 200

        blocked_sell = client.post(
            "/api/inventory/sell",
            json={"part_id": "SENSOR-9", "quantity": 2, "actor": "user"},
        )
        assert blocked_sell.status_code == 409

        state = client.get("/api/state").json()
        sensor = item_by_id(state["inventory"], "SENSOR-9")

    assert sensor["on_hand"] == 3
    assert sensor["available"] == 1
    assert len(state["reservations"]) == 1
    assert len(state["audit_log"]) == 4


def test_base_checkpoint_api_shapes_branch_creation(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        base_response = client.post("/api/bases", json={"label": "test-base"})
        assert base_response.status_code == 200
        base = base_response.json()["base"]

        bases = client.get("/api/bases")
        assert bases.status_code == 200
        assert bases.json()["bases"][0]["id"] == base["id"]

        branch_response = client.post(f"/api/bases/{base['id']}/branches")
        assert branch_response.status_code == 200
        branch = branch_response.json()["branch"]

        agent_response = client.post(f"/api/branches/{branch['id']}/run-agent-demo")
        assert agent_response.status_code == 200
        snapshots = agent_response.json()["snapshots"]
        diff = agent_response.json()["diff"]

        branches = client.get("/api/branches")
        assert branches.status_code == 200

        backend = client.get("/api/backend")
        assert backend.status_code == 200

        blocked_delete = client.delete(f"/api/bases/{base['id']}")
        assert blocked_delete.status_code == 400

        client.post(f"/api/branches/{branch['id']}/discard")
        deleted = client.delete(f"/api/bases/{base['id']}")
        assert deleted.status_code == 200

    assert base["label"] == "test-base"
    assert branch["base_id"] == base["id"]
    assert branch["base_checkpoint_id"] == base["checkpoint_id"]
    assert branches.json()["branches"][0]["base_id"] == base["id"]
    assert [snapshot["action"] for snapshot in snapshots] == ["sell", "buy", "reserve"]
    assert [snapshot["label"] for snapshot in snapshots] == [
        "Sell 3 CASE-42",
        "Buy 5 SENSOR-9",
        "Reserve 2 MCU-100",
    ]
    assert snapshots[0]["parent_id"] == base["checkpoint_id"]
    assert snapshots[1]["parent_id"] == snapshots[0]["id"]
    assert snapshots[2]["parent_id"] == snapshots[1]["id"]
    assert diff["inventory"] == [
        {
            "part_id": "CASE-42",
            "on_hand_delta": -3,
            "available_delta": -3,
            "reserved_delta": 0,
        },
        {
            "part_id": "MCU-100",
            "on_hand_delta": 0,
            "available_delta": -2,
            "reserved_delta": 2,
        },
        {
            "part_id": "SENSOR-9",
            "on_hand_delta": 5,
            "available_delta": 5,
            "reserved_delta": 0,
        },
    ]
    assert len(branches.json()["branches"][0]["snapshots"]) == 3
    backend_status = backend.json()
    assert backend_status["backend"] == "local-copy"
    assert backend_status["method"] == "file-copy"
    assert backend_status["totals"] == {"bases": 1, "branches": 1, "snapshots": 3}
    assert backend_status["operations"]["snapshot"]["count"] >= 4
    assert backend_status["operations"]["restore"]["count"] >= 1
    assert deleted.json()["status"] == "deleted"


def test_reset_clears_bases_and_branches(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        base = client.post("/api/bases", json={"label": "reset-base"}).json()["base"]
        branch = client.post(f"/api/bases/{base['id']}/branches").json()["branch"]

        before_reset = client.get("/api/branches").json()
        assert before_reset["branches"][0]["id"] == branch["id"]

        reset = client.post("/api/reset")
        assert reset.status_code == 200

        bases = client.get("/api/bases")
        branches = client.get("/api/branches")
        backend = client.get("/api/backend")
        state = client.get("/api/state")

    assert reset.json()["cleanup"] == {"branches_deleted": 1, "bases_deleted": 1}
    assert bases.json()["bases"] == []
    assert branches.json()["branches"] == []
    assert backend.json()["totals"] == {"bases": 0, "branches": 0, "snapshots": 0}
    assert backend.json()["operations"]["snapshot"]["count"] == 0
    assert backend.json()["operations"]["restore"]["count"] == 0
    assert len(state.json()["reservations"]) == 0
    assert len(state.json()["audit_log"]) == 1
