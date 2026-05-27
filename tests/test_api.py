import importlib
import json
import sys
from pathlib import Path
from urllib import request as urlrequest

import pytest
from fastapi.testclient import TestClient

from agent_safe_demo.branching import (
    BranchError,
    BranchHandle,
    CheckpointLiteBackend,
    StateForkBackend,
)


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


def get_json(url: str) -> dict:
    with urlrequest.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


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


def test_label_message_creates_one_label_and_audit_event(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/messages/msg-1001/label",
            json={"label": "Finance", "actor": "alice"},
        )
        duplicate = client.post(
            "/api/messages/msg-1001/label",
            json={"label": "finance", "actor": "alice"},
        )
        state = client.get("/api/state").json()

    assert response.status_code == 200
    assert duplicate.status_code == 200
    assert response.json()["status"] == "labeled"
    assert duplicate.json()["status"] == "unchanged"
    message = next(message for message in state["messages"] if message["id"] == "msg-1001")
    assert message["labels"].count("finance") == 1
    label_events = [
        event for event in state["audit_log"]
        if event["action"] == "label" and "msg-1001" in event["detail"]
    ]
    assert len(label_events) == 1


def test_move_and_read_message_update_state_and_audit_log(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        move = client.post(
            "/api/messages/msg-1003/move",
            json={"folder": "Spam", "actor": "moderator"},
        )
        read = client.post(
            "/api/messages/msg-1003/read",
            json={"is_read": True, "actor": "moderator"},
        )
        state = client.get("/api/state").json()

    assert move.status_code == 200
    assert read.status_code == 200
    message = next(message for message in state["messages"] if message["id"] == "msg-1003")
    assert message["folder"] == "Spam"
    assert message["is_read"] is True
    assert any(event["action"] == "move" and "Spam" in event["detail"] for event in state["audit_log"])
    assert any(event["action"] == "read" and "msg-1003" in event["detail"] for event in state["audit_log"])


def test_archive_message_accepts_empty_body(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/messages/msg-1004/archive")
        state = client.get("/api/state").json()

    assert response.status_code == 200
    message = next(message for message in state["messages"] if message["id"] == "msg-1004")
    assert message["folder"] == "Archive"
    assert any(event["action"] == "archive" and "msg-1004" in event["detail"] for event in state["audit_log"])


def test_create_draft_increments_mailbox_draft_count(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        before = client.get("/api/mailbox").json()
        response = client.post(
            "/api/drafts",
            json={
                "source_message_id": "msg-1002",
                "to_address": "customer@acme.example",
                "subject": "Re: Urgent: shipment delay",
                "body": "Thanks for the heads up. I will send a new ETA shortly.",
                "created_by": "support",
            },
        )
        after = client.get("/api/mailbox").json()
        state = client.get("/api/state").json()

    assert response.status_code == 200
    assert response.json()["draft"]["source_message_id"] == "msg-1002"
    assert after["drafts"] == before["drafts"] + 1
    assert any(draft["created_by"] == "support" for draft in state["drafts"])
    assert any(event["action"] == "draft" for event in state["audit_log"])


def test_create_message_adds_email_to_mailbox(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/messages",
            json={
                "id": "msg-test-2001",
                "from_address": "director@example.com",
                "to_address": "ops@example.com",
                "subject": "Follow-up: customer escalation",
                "body": "Please keep the customer updated.",
                "folder": "Inbox",
                "priority": "high",
                "actor": "agent",
            },
        )
        mailbox = client.get("/api/mailbox").json()
        state = client.get("/api/state").json()

    assert response.status_code == 200
    assert response.json()["status"] == "received"
    messages = {message["id"]: message for message in mailbox["messages"]}
    assert messages["msg-test-2001"]["subject"] == "Follow-up: customer escalation"
    assert messages["msg-test-2001"]["folder"] == "Inbox"
    assert messages["msg-test-2001"]["is_read"] is False
    assert any(
        event["action"] == "receive" and "msg-test-2001" in event["detail"]
        for event in state["audit_log"]
    )


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


def test_branch_agent_demo_runs_email_plan_without_changing_main(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    branch = None
    with TestClient(app) as client:
        base = client.post("/api/bases", json={"label": "agent-base"}).json()["base"]
        branch = client.post(f"/api/bases/{base['id']}/branches").json()["branch"]
        try:
            agent = client.post(f"/api/branches/{branch['id']}/run-agent-demo")
            branch_state = get_json(f"{branch['url']}/api/state")
            main_state = client.get("/api/state").json()
        finally:
            client.post(f"/api/branches/{branch['id']}/discard")

    assert agent.status_code == 200
    payload = agent.json()
    assert payload["snapshots"] == []
    assert payload["branch"]["dirty"] is True

    branch_messages = {message["id"]: message for message in branch_state["messages"]}
    assert "finance" in branch_messages["msg-1001"]["labels"]
    assert branch_messages["msg-1003"]["folder"] == "Spam"
    assert branch_messages["msg-1004"]["folder"] == "Archive"
    assert branch_messages["msg-agent-2001"]["subject"] == "Follow-up: customer escalation"
    assert branch_messages["msg-agent-2001"]["folder"] == "Inbox"
    assert any(
        draft["source_message_id"] == "msg-1002"
        and draft["created_by"] == "agent"
        and "new ETA shortly" in draft["body"]
        for draft in branch_state["drafts"]
    )

    main_messages = {message["id"]: message for message in main_state["messages"]}
    assert "finance" not in main_messages["msg-1001"]["labels"]
    assert main_messages["msg-1003"]["folder"] == "Inbox"
    assert main_messages["msg-1004"]["folder"] == "Inbox"
    assert "msg-agent-2001" not in main_messages
    assert len(main_state["drafts"]) == 1


def test_manual_snapshot_restore_requires_dirty_choice(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    branch = None
    with TestClient(app) as client:
        base = client.post("/api/bases", json={"label": "restore-base"}).json()["base"]
        branch = client.post(f"/api/bases/{base['id']}/branches").json()["branch"]
        try:
            clean_snapshot = client.post(
                f"/api/branches/{branch['id']}/snapshots",
                json={"label": "clean mailbox"},
            ).json()["snapshot"]
            agent = client.post(f"/api/branches/{branch['id']}/run-agent-demo")
            dirty = client.get(f"/api/branches/{branch['id']}/dirty")
            blocked_restore = client.post(
                f"/api/branches/{branch['id']}/restore",
                json={"snapshot_id": clean_snapshot["id"]},
            )
            restored = client.post(
                f"/api/branches/{branch['id']}/restore",
                json={"snapshot_id": clean_snapshot["id"], "force": True},
            )
            branch_state = get_json(f"{branch['url']}/api/state")
            backend = client.get("/api/backend").json()
        finally:
            client.post(f"/api/branches/{branch['id']}/discard")

    assert agent.status_code == 200
    assert dirty.json()["dirty"] is True
    assert blocked_restore.status_code == 409
    assert restored.status_code == 200
    assert restored.json()["branch"]["dirty"] is False
    assert restored.json()["branch"]["current_snapshot_id"] == clean_snapshot["id"]
    assert backend["totals"]["snapshots"] == 1

    branch_messages = {message["id"]: message for message in branch_state["messages"]}
    assert "finance" not in branch_messages["msg-1001"]["labels"]
    assert branch_messages["msg-1003"]["folder"] == "Inbox"
    assert branch_messages["msg-1004"]["folder"] == "Inbox"
    assert "msg-agent-2001" not in branch_messages
    assert len(branch_state["drafts"]) == 1


def test_workspace_starts_in_runtime_with_initial_checkpoint(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        try:
            response = client.get("/api/workspace")
            assert response.status_code == 200
            workspace = response.json()
            branch = workspace["branch"]
            runtime_mailbox = get_json(f"{branch['url']}/api/mailbox")
            backend = client.get("/api/backend").json()
        finally:
            client.post("/api/reset")

    assert workspace["workspace"]["mode"] == "runtime-checkpoints"
    assert workspace["workspace"]["runtime_url"] == branch["url"]
    assert branch["status"] == "running"
    assert branch["dirty"] is False
    assert [snapshot["label"] for snapshot in branch["snapshots"]] == [
        "Initial checkpoint"
    ]
    assert runtime_mailbox["unread"] == 3
    assert runtime_mailbox["drafts"] == 1
    assert backend["totals"] == {"bases": 1, "branches": 1, "snapshots": 1}


def test_workspace_agent_and_restore_keep_main_as_seed(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        workspace = client.get("/api/workspace").json()
        branch = workspace["branch"]
        initial_snapshot = branch["snapshots"][0]
        try:
            agent = client.post("/api/workspace/run-agent")
            dirty = client.get("/api/workspace/dirty")
            branch_state = get_json(f"{branch['url']}/api/state")
            main_state = client.get("/api/state").json()
            blocked_restore = client.post(
                "/api/workspace/restore",
                json={"snapshot_id": initial_snapshot["id"]},
            )
            restored = client.post(
                "/api/workspace/restore",
                json={"snapshot_id": initial_snapshot["id"], "force": True},
            )
            restored_state = get_json(f"{restored.json()['branch']['url']}/api/state")
        finally:
            client.post("/api/reset")

    assert agent.status_code == 200
    assert dirty.json()["dirty"] is True
    assert blocked_restore.status_code == 409
    assert restored.status_code == 200
    assert restored.json()["branch"]["dirty"] is False

    branch_messages = {message["id"]: message for message in branch_state["messages"]}
    assert "msg-agent-2001" in branch_messages
    assert branch_messages["msg-agent-2001"]["folder"] == "Inbox"
    assert branch_messages["msg-1003"]["folder"] == "Spam"
    assert any(
        draft["source_message_id"] == "msg-1002"
        and draft["created_by"] == "agent"
        and "new ETA shortly" in draft["body"]
        for draft in branch_state["drafts"]
    )

    main_messages = {message["id"]: message for message in main_state["messages"]}
    assert "msg-agent-2001" not in main_messages
    assert main_messages["msg-1003"]["folder"] == "Inbox"
    assert len(main_state["drafts"]) == 1

    restored_messages = {message["id"]: message for message in restored_state["messages"]}
    assert "msg-agent-2001" not in restored_messages
    assert restored_messages["msg-1003"]["folder"] == "Inbox"
    assert len(restored_state["drafts"]) == 1


def test_commit_promotes_branch_when_main_still_matches_base(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    branch = None
    with TestClient(app) as client:
        base = client.post("/api/bases", json={"label": "commit-base"}).json()["base"]
        branch = client.post(f"/api/bases/{base['id']}/branches").json()["branch"]
        agent = client.post(f"/api/branches/{branch['id']}/run-agent-demo")
        commit = client.post(f"/api/branches/{branch['id']}/commit")
        main_state = client.get("/api/state").json()
        branches = client.get("/api/branches").json()["branches"]

    assert agent.status_code == 200
    assert commit.status_code == 200
    assert commit.json()["status"] == "committed"
    assert branches == []

    main_messages = {message["id"]: message for message in main_state["messages"]}
    assert "finance" in main_messages["msg-1001"]["labels"]
    assert main_messages["msg-1003"]["folder"] == "Spam"
    assert main_messages["msg-1004"]["folder"] == "Archive"
    assert main_messages["msg-agent-2001"]["subject"] == "Follow-up: customer escalation"
    assert any(
        draft["source_message_id"] == "msg-1002"
        and draft["created_by"] == "agent"
        and "new ETA shortly" in draft["body"]
        for draft in main_state["drafts"]
    )


def test_commit_rejects_branch_when_main_changed_after_base(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    branch = None
    with TestClient(app) as client:
        base = client.post("/api/bases", json={"label": "stale-base"}).json()["base"]
        branch = client.post(f"/api/bases/{base['id']}/branches").json()["branch"]

        user_change = client.post(
            "/api/messages/msg-1001/label",
            json={"label": "user-work", "actor": "user"},
        )
        agent = client.post(f"/api/branches/{branch['id']}/run-agent-demo")
        commit = client.post(f"/api/branches/{branch['id']}/commit")
        main_state = client.get("/api/state").json()
        branches = client.get("/api/branches").json()["branches"]
        client.post(f"/api/branches/{branch['id']}/discard")

    assert user_change.status_code == 200
    assert agent.status_code == 200
    assert commit.status_code == 400
    assert "Main state changed after this branch base was created" in commit.json()["detail"]
    assert [active["id"] for active in branches] == [branch["id"]]

    main_messages = {message["id"]: message for message in main_state["messages"]}
    assert "user-work" in main_messages["msg-1001"]["labels"]
    assert "finance" not in main_messages["msg-1001"]["labels"]
    assert main_messages["msg-1003"]["folder"] == "Inbox"
    assert main_messages["msg-1004"]["folder"] == "Inbox"
    assert "msg-agent-2001" not in main_messages


@pytest.mark.parametrize(
    ("backend_cls", "backend_name"),
    [
        (CheckpointLiteBackend, "checkpoint-lite"),
        (StateForkBackend, "statefork"),
    ],
)
def test_checkpoint_backends_reject_concurrent_active_branch(backend_cls, backend_name):
    backend = object.__new__(backend_cls)
    backend.name = backend_name
    backend.branches = {
        "active-branch": BranchHandle(
            id="active-branch",
            backend=backend_name,
            db_path=Path("branch.db"),
            port=8300,
            url="http://127.0.0.1:8300",
            status="running",
        )
    }

    with pytest.raises(BranchError, match="one active branch at a time"):
        backend_cls.create_branch(backend, base_id="base-1")


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
