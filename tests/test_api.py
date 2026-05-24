import importlib
import sys

from fastapi.testclient import TestClient


def load_app(monkeypatch, tmp_path, auth_password=None):
    db_path = tmp_path / "toy_mailbox.db"
    monkeypatch.setenv("TOY_MAILBOX_DB_PATH", str(db_path))
    monkeypatch.setenv("TOY_BRANCH_BACKEND", "local-copy")
    monkeypatch.delenv("TOY_MAILBOX_BRANCH_ID", raising=False)
    if auth_password is None:
        monkeypatch.delenv("TOY_DEMO_AUTH_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("TOY_DEMO_AUTH_PASSWORD", auth_password)
    sys.modules.pop("agent_safe_demo.main", None)
    module = importlib.import_module("agent_safe_demo.main")
    return module.app


def test_mailbox_seed_data(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/mailbox")

    assert response.status_code == 200
    mailbox = response.json()
    messages = mailbox["messages"]
    assert {message["id"] for message in messages} >= {"msg-1001", "msg-1002"}
    assert mailbox["unread"] == 3
    assert mailbox["drafts"] == 1
    assert {"folder": "Inbox", "count": 4} in mailbox["folders"]


def test_message_detail_includes_labels(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/messages/msg-1002")

    assert response.status_code == 200
    message = response.json()["message"]
    assert message["subject"] == "Urgent: shipment delay"
    assert message["is_read"] is False
    assert set(message["labels"]) == {"customer", "urgent"}


def test_demo_password_protects_main_app(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path, auth_password="secret-demo-password")
    with TestClient(app) as client:
        blocked = client.get("/api/mailbox")
        wrong_password = client.get("/api/mailbox", auth=("demo", "wrong"))
        allowed = client.get("/api/mailbox", auth=("demo", "secret-demo-password"))

    assert blocked.status_code == 401
    assert blocked.headers["www-authenticate"] == 'Basic realm="Agent-Safe Demo"'
    assert wrong_password.status_code == 401
    assert allowed.status_code == 200


def test_base_checkpoint_api_shapes_branch_creation(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        base_response = client.post("/api/bases", json={"label": "mailbox-base"})
        assert base_response.status_code == 200
        base = base_response.json()["base"]

        bases = client.get("/api/bases")
        assert bases.status_code == 200
        assert bases.json()["bases"][0]["id"] == base["id"]

        branch_response = client.post(f"/api/bases/{base['id']}/branches")
        assert branch_response.status_code == 200
        branch = branch_response.json()["branch"]

        branches = client.get("/api/branches")
        assert branches.status_code == 200

        backend = client.get("/api/backend")
        assert backend.status_code == 200

        blocked_delete = client.delete(f"/api/bases/{base['id']}")
        assert blocked_delete.status_code == 400

        discarded = client.post(f"/api/branches/{branch['id']}/discard")
        assert discarded.status_code == 200
        deleted = client.delete(f"/api/bases/{base['id']}")
        assert deleted.status_code == 200

    assert base["label"] == "mailbox-base"
    assert branch["base_id"] == base["id"]
    assert branch["base_checkpoint_id"] == base["checkpoint_id"]
    assert branches.json()["branches"][0]["base_id"] == base["id"]
    assert branches.json()["branches"][0]["snapshots"] == []
    backend_status = backend.json()
    assert backend_status["backend"] == "local-copy"
    assert backend_status["method"] == "file-copy"
    assert backend_status["totals"] == {"bases": 1, "branches": 1, "snapshots": 0}
    assert backend_status["operations"]["snapshot"]["count"] >= 1
    assert backend_status["operations"]["restore"]["count"] >= 1
    assert deleted.json()["status"] == "deleted"


def test_reset_clears_bases_branches_and_mailbox_state(monkeypatch, tmp_path):
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
    assert len(state.json()["messages"]) == 5
    assert len(state.json()["drafts"]) == 1
    assert len(state.json()["audit_log"]) == 1
