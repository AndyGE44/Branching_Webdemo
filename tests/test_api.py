import importlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib import request as urlrequest

import pytest
from fastapi.testclient import TestClient

from agent_safe_demo.control_plane.branching import (
    BaseHandle,
    BranchError,
    BranchHandle,
    StateForkBackend,
)
from agent_safe_demo.control_plane.commit_store import CommitStore
from agent_safe_demo.control_plane.manifest import interpolate_template, load_manifest


RUN_STATEFORK_INTEGRATION = os.getenv("RUN_STATEFORK_INTEGRATION") == "1"
requires_statefork_integration = pytest.mark.skipif(
    not RUN_STATEFORK_INTEGRATION,
    reason="requires real StateFork integration",
)

def configure_env(monkeypatch, tmp_path, auth_password=None) -> None:
    db_path = tmp_path / "demo_mailbox.db"
    inventory_db_path = tmp_path / "demo_inventory.db"
    control_plane_db_path = tmp_path / "control_plane_metadata.db"
    monkeypatch.setenv("DEMO_MAILBOX_DB_PATH", str(db_path))
    monkeypatch.setenv("DEMO_INVENTORY_DB_PATH", str(inventory_db_path))
    monkeypatch.setenv("DEMO_CONTROL_PLANE_DB_PATH", str(control_plane_db_path))
    monkeypatch.delenv("DEMO_APP_ID", raising=False)
    monkeypatch.setenv(
        "DEMO_STATEFORK_ROOT",
        os.getenv("DEMO_STATEFORK_ROOT", "/users/alexxjk/StateFork"),
    )
    monkeypatch.setenv(
        "DEMO_STATEFORK_CWD",
        os.getenv("DEMO_STATEFORK_CWD", "/users/alexxjk/StateFork"),
    )
    monkeypatch.setenv("DEMO_STATEFORK_METHOD", os.getenv("DEMO_STATEFORK_METHOD", "ckpt_build"))
    monkeypatch.setenv("DEMO_BRANCH_PORT_START", os.getenv("DEMO_BRANCH_PORT_START", "8300"))
    monkeypatch.delenv("DEMO_STATEFORK_BUILD", raising=False)
    monkeypatch.delenv("DEMO_MAILBOX_BRANCH_ID", raising=False)
    if auth_password is None:
        monkeypatch.delenv("DEMO_AUTH_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("DEMO_AUTH_PASSWORD", auth_password)


def load_mailbox_app(monkeypatch, tmp_path, auth_password=None):
    configure_env(monkeypatch, tmp_path, auth_password)
    sys.modules.pop("agent_safe_demo.app_plane.email_service.app", None)
    sys.modules.pop("agent_safe_demo.mailbox_app", None)
    module = importlib.import_module("agent_safe_demo.app_plane.email_service.app")
    return module.app


def load_inventory_app(monkeypatch, tmp_path, auth_password=None):
    configure_env(monkeypatch, tmp_path, auth_password)
    sys.modules.pop("agent_safe_demo.app_plane.inventory_service.app", None)
    module = importlib.import_module("agent_safe_demo.app_plane.inventory_service.app")
    return module.app


def load_controller_app(monkeypatch, tmp_path, auth_password=None):
    configure_env(monkeypatch, tmp_path, auth_password)
    for module_name in [
        "agent_safe_demo.control_plane.main",
        "agent_safe_demo.control_plane.app_registry",
        "agent_safe_demo.control_plane.manifest",
        "agent_safe_demo.app_plane.email_service.app",
        "agent_safe_demo.app_plane.inventory_service.app",
        "agent_safe_demo.main",
        "agent_safe_demo.mailbox_app",
    ]:
        sys.modules.pop(module_name, None)
    module = importlib.import_module("agent_safe_demo.control_plane.main")
    module.reset_workspace_handles()
    return module.app


def mailbox_state() -> dict:
    module = importlib.import_module("agent_safe_demo.app_plane.email_service.app")
    return module.state()


def get_json(url: str) -> dict:
    with urlrequest.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def test_legacy_entrypoints_delegate_to_new_planes(monkeypatch, tmp_path):
    configure_env(monkeypatch, tmp_path)
    for module_name in [
        "agent_safe_demo.main",
        "agent_safe_demo.mailbox_app",
        "agent_safe_demo.branching",
        "agent_safe_demo.control_plane.main",
        "agent_safe_demo.app_plane.email_service.app",
    ]:
        sys.modules.pop(module_name, None)

    legacy_controller = importlib.import_module("agent_safe_demo.main")
    control_plane = importlib.import_module("agent_safe_demo.control_plane.main")
    legacy_mailbox = importlib.import_module("agent_safe_demo.mailbox_app")
    email_service = importlib.import_module("agent_safe_demo.app_plane.email_service.app")
    legacy_branching = importlib.import_module("agent_safe_demo.branching")

    assert legacy_controller.app is control_plane.app
    assert legacy_mailbox.app is email_service.app
    assert legacy_branching.BranchError is BranchError


def test_mailbox_seed_data(monkeypatch, tmp_path):
    app = load_mailbox_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/mailbox")

    assert response.status_code == 200
    mailbox = response.json()
    messages = mailbox["messages"]
    assert {message["id"] for message in messages} >= {"msg-1001", "msg-1002"}
    assert mailbox["unread"] == 3
    assert mailbox["drafts"] == 1
    assert {"folder": "Inbox", "count": 4} in mailbox["folders"]


def test_inventory_app_seed_and_reservation(monkeypatch, tmp_path):
    app = load_inventory_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        inventory = client.get("/api/inventory")
        reservation = client.post(
            "/api/reservations",
            json={"part_id": "MCU-100", "quantity": 2, "actor": "tester"},
        )
        state = client.get("/api/state")

    assert inventory.status_code == 200
    assert {item["id"] for item in inventory.json()["items"]} >= {"MCU-100", "SENSOR-9"}
    assert reservation.status_code == 200
    assert reservation.json()["status"] == "active"
    assert state.json()["summary"]["reservations"] == 1


def test_commit_store_records_app_heads(tmp_path):
    store = CommitStore(tmp_path / "metadata.db")

    first = store.create_commit(
        app_id="email",
        parent_commit_id=None,
        base_id="base-1",
        branch_id="branch-1",
        checkpoint_id="checkpoint-1",
        label="first",
        message="seed accepted",
        author="tester",
        diff={"tables": ["messages"]},
    )
    second = store.create_commit(
        app_id="email",
        parent_commit_id=first["id"],
        base_id="base-1",
        branch_id="branch-2",
        checkpoint_id="checkpoint-2",
        label="second",
        diff={"tables": ["drafts"]},
    )

    assert store.app_head("email")["id"] == second["id"]
    assert store.app_head("email")["parent_commit_id"] == first["id"]
    assert [commit["id"] for commit in store.list_commits("email")] == [
        second["id"],
        first["id"],
    ]
    assert store.list_commits("inventory") == []


def test_statefork_manifest_loads_runtime_contract():
    manifest = load_manifest(
        Path("src/agent_safe_demo/app_plane/email_service/statefork.yaml")
    )

    assert manifest.id == "email"
    assert manifest.name == "Email Service"
    assert "agent_safe_demo.app_plane.email_service.app:app" in manifest.runtime.command
    assert manifest.runtime.cwd == "${BRANCH_WORKDIR}"
    assert manifest.runtime.port_env == "PORT"
    assert manifest.state.files == ["demo_mailbox.db"]
    assert manifest.state.env == {
        "DEMO_MAILBOX_DB_PATH": "${BRANCH_WORKDIR}/demo_mailbox.db"
    }
    assert manifest.observability.state_summary_path == "/api/state"
    assert (
        interpolate_template(
            "${BRANCH_WORKDIR}:${PORT}:${MISSING}",
            {"BRANCH_WORKDIR": "/tmp/branch", "PORT": 8300},
        )
        == "/tmp/branch:8300:${MISSING}"
    )


def test_statefork_manifest_validation_errors_are_readable(tmp_path):
    manifest_path = tmp_path / "statefork.yaml"
    manifest_path.write_text("id: broken\nruntime: {}\n")

    with pytest.raises(ValueError, match="Invalid manifest"):
        load_manifest(manifest_path)


def test_app_registry_discovers_manifests_and_reports_errors(tmp_path):
    from agent_safe_demo.control_plane import app_registry

    specs = app_registry.build_app_specs()
    assert set(specs) == {"email", "inventory"}
    assert specs["email"].manifest_path.name == "statefork.yaml"
    assert specs["email"].state_files == ("demo_mailbox.db",)
    assert "agent_safe_demo.app_plane.email_service.app:app" in specs["email"].runtime_command
    assert app_registry.list_manifest_errors() == []

    unsupported = tmp_path / "notes_service"
    unsupported.mkdir()
    (unsupported / "statefork.yaml").write_text(
        """
        id: notes
        name: Notes
        description: Unsupported app
        runtime:
          command: "python -m uvicorn notes:app --port ${PORT}"
          cwd: "."
          port_env: "PORT"
          health_path: "/health"
          ui_path: "/"
        state:
          files: ["notes.db"]
          env: {}
        observability:
          state_summary_path: "/state"
        """
    )

    fallback_specs = app_registry.build_app_specs(app_plane_dir=tmp_path)
    errors = app_registry.list_manifest_errors(app_plane_dir=tmp_path)

    assert set(fallback_specs) == {"email", "inventory"}
    assert fallback_specs["email"].manifest_path is None
    assert len(errors) == 1
    assert "No Python adapter registered" in errors[0]["error"]


def test_controller_lists_and_switches_registered_apps(monkeypatch, tmp_path):
    app = load_controller_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        apps = client.get("/api/apps")
        selected = client.post("/api/apps/inventory/select")
        backend = client.get("/api/backend")

    assert apps.status_code == 200
    payload = apps.json()
    assert payload["current_app_id"] == "email"
    assert payload["manifest_errors"] == []
    assert {app["id"] for app in payload["apps"]} == {"email", "inventory"}
    email_app = next(app for app in payload["apps"] if app["id"] == "email")
    assert email_app["manifest_loaded"] is True
    assert email_app["state_files"] == ["demo_mailbox.db"]
    assert "agent_safe_demo.app_plane.email_service.app:app" in email_app["runtime_command"]
    assert selected.status_code == 200
    assert selected.json()["current_app_id"] == "inventory"
    backend_details = backend.json()["details"]
    assert backend_details["app_id"] == "inventory"
    assert backend_details["app_db_env_var"] == "DEMO_INVENTORY_DB_PATH"
    assert backend_details["manifest_path"].endswith("inventory_service/statefork.yaml")
    assert backend_details["runtime_port_env"] == "PORT"
    assert backend_details["state_files"][0]["name"] == "demo_inventory.db"


def test_workspace_payload_uses_same_origin_runtime_proxy(monkeypatch, tmp_path):
    configure_env(monkeypatch, tmp_path)
    sys.modules.pop("agent_safe_demo.control_plane.main", None)
    module = importlib.import_module("agent_safe_demo.control_plane.main")
    branch = {"id": "branch-1", "url": "http://127.0.0.1:8300", "snapshots": []}

    workspace = module.workspace_payload(branch)["workspace"]

    assert workspace["runtime_url"] == "http://127.0.0.1:8300"
    assert workspace["runtime_proxy_url"] == "/runtime"
    assert workspace["runtime_ui_url"] == "/runtime/"
    assert module.runtime_target_url(branch, "api/state", b"page=1") == (
        "http://127.0.0.1:8300/api/state?page=1"
    )


def test_workspace_payload_includes_active_app_head(monkeypatch, tmp_path):
    configure_env(monkeypatch, tmp_path)
    sys.modules.pop("agent_safe_demo.control_plane.main", None)
    module = importlib.import_module("agent_safe_demo.control_plane.main")
    commit = module.commit_store.create_commit(
        app_id="email",
        parent_commit_id=None,
        base_id="base-1",
        branch_id="branch-1",
        checkpoint_id="checkpoint-1",
        label="accepted mailbox",
        diff={"tables": ["messages"]},
    )
    module.branch_backend.head_base_id = "base-1"
    branch = {
        "id": "branch-2",
        "base_id": "base-1",
        "url": "http://127.0.0.1:8300",
        "snapshots": [],
    }

    payload = module.workspace_payload(branch)

    assert payload["app_head"]["id"] == commit["id"]
    assert payload["app_head"]["active"] is True
    assert payload["workspace"]["head_commit_id"] == commit["id"]
    assert payload["commits"][0]["diff"] == {"tables": ["messages"]}

    module.branch_backend.head_base_id = "base-2"
    inactive_payload = module.workspace_payload(branch)
    assert inactive_payload["app_head"]["active"] is False
    assert inactive_payload["workspace"]["head_commit_id"] is None


def test_message_detail_includes_labels(monkeypatch, tmp_path):
    app = load_mailbox_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/messages/msg-1002")

    assert response.status_code == 200
    message = response.json()["message"]
    assert message["subject"] == "Urgent: shipment delay"
    assert message["is_read"] is False
    assert set(message["labels"]) == {"customer", "urgent"}


def test_label_message_creates_one_label_and_audit_event(monkeypatch, tmp_path):
    app = load_mailbox_app(monkeypatch, tmp_path)
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
    app = load_mailbox_app(monkeypatch, tmp_path)
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
    app = load_mailbox_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/messages/msg-1004/archive")
        state = client.get("/api/state").json()

    assert response.status_code == 200
    message = next(message for message in state["messages"] if message["id"] == "msg-1004")
    assert message["folder"] == "Archive"
    assert any(event["action"] == "archive" and "msg-1004" in event["detail"] for event in state["audit_log"])


def test_create_draft_increments_mailbox_draft_count(monkeypatch, tmp_path):
    app = load_mailbox_app(monkeypatch, tmp_path)
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
    app = load_mailbox_app(monkeypatch, tmp_path)
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


def test_demo_password_protects_controller_app(monkeypatch, tmp_path):
    app = load_controller_app(monkeypatch, tmp_path, auth_password="secret-demo-password")
    with TestClient(app) as client:
        blocked = client.get("/api/backend")
        wrong_password = client.get("/api/backend", auth=("demo", "wrong"))
        allowed = client.get("/api/backend", auth=("demo", "secret-demo-password"))

    assert blocked.status_code == 401
    assert blocked.headers["www-authenticate"] == 'Basic realm="Agent-Safe Demo"'
    assert wrong_password.status_code == 401
    assert allowed.status_code == 200


def test_business_and_control_apis_are_separate(monkeypatch, tmp_path):
    mailbox = load_mailbox_app(monkeypatch, tmp_path)
    controller = load_controller_app(monkeypatch, tmp_path)
    with TestClient(mailbox) as mailbox_client, TestClient(controller) as controller_client:
        mailbox_business = mailbox_client.get("/api/mailbox")
        mailbox_control = mailbox_client.get("/api/workspace")
        controller_control = controller_client.get("/api/backend")
        controller_business = controller_client.get("/api/mailbox")

    assert mailbox_business.status_code == 200
    assert mailbox_control.status_code == 404
    assert controller_control.status_code == 200
    assert controller_business.status_code == 404


@requires_statefork_integration
def test_base_checkpoint_api_shapes_branch_creation(monkeypatch, tmp_path):
    app = load_controller_app(monkeypatch, tmp_path)
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
    assert backend_status["backend"] == "statefork"
    assert backend_status["method"] == "statefork:ckpt_build"
    assert backend_status["totals"] == {"bases": 1, "branches": 1, "snapshots": 0}
    assert backend_status["operations"]["snapshot"]["count"] >= 1
    assert backend_status["operations"]["restore"]["count"] >= 1
    assert deleted.json()["status"] == "deleted"


@requires_statefork_integration
def test_branch_agent_demo_runs_email_plan_without_changing_main(monkeypatch, tmp_path):
    app = load_controller_app(monkeypatch, tmp_path)
    branch = None
    with TestClient(app) as client:
        base = client.post("/api/bases", json={"label": "agent-base"}).json()["base"]
        branch = client.post(f"/api/bases/{base['id']}/branches").json()["branch"]
        try:
            agent = client.post(f"/api/branches/{branch['id']}/run-agent-demo")
            branch_state = get_json(f"{branch['url']}/api/state")
            main_state = mailbox_state()
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


@requires_statefork_integration
def test_manual_snapshot_restore_requires_dirty_choice(monkeypatch, tmp_path):
    app = load_controller_app(monkeypatch, tmp_path)
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


@requires_statefork_integration
def test_workspace_starts_in_runtime_with_initial_checkpoint(monkeypatch, tmp_path):
    app = load_controller_app(monkeypatch, tmp_path)
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


@requires_statefork_integration
def test_workspace_commit_records_metadata_and_reopens_from_head(monkeypatch, tmp_path):
    app = load_controller_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        try:
            workspace = client.get("/api/workspace").json()
            initial_branch_id = workspace["branch"]["id"]
            agent = client.post("/api/workspace/run-agent")
            commit = client.post(
                "/api/workspace/commit",
                json={"label": "accept agent plan", "message": "looks good"},
            )
            commits = client.get("/api/workspace/commits").json()
            new_branch = commit.json()["branch"]
            committed_state = get_json(f"{new_branch['url']}/api/state")
            main_state = mailbox_state()
        finally:
            client.post("/api/reset")

    assert agent.status_code == 200
    assert commit.status_code == 200
    payload = commit.json()
    assert payload["status"] == "committed"
    assert payload["commit"]["label"] == "accept agent plan"
    assert payload["commit"]["message"] == "looks good"
    assert payload["commit"]["branch_id"] == initial_branch_id
    assert payload["app_head"]["active"] is True
    assert payload["workspace"]["head_commit_id"] == payload["commit"]["id"]
    assert new_branch["id"] != initial_branch_id
    assert commits["head"]["id"] == payload["commit"]["id"]
    assert commits["commits"][0]["diff"]["tables"]

    committed_messages = {message["id"]: message for message in committed_state["messages"]}
    assert "finance" in committed_messages["msg-1001"]["labels"]
    assert committed_messages["msg-1003"]["folder"] == "Spam"
    assert "msg-agent-2001" in committed_messages

    main_messages = {message["id"]: message for message in main_state["messages"]}
    assert "finance" not in main_messages["msg-1001"]["labels"]
    assert "msg-agent-2001" not in main_messages


@requires_statefork_integration
def test_workspace_agent_and_restore_keep_main_as_seed(monkeypatch, tmp_path):
    app = load_controller_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        workspace = client.get("/api/workspace").json()
        branch = workspace["branch"]
        initial_snapshot = branch["snapshots"][0]
        try:
            agent = client.post("/api/workspace/run-agent")
            dirty = client.get("/api/workspace/dirty")
            branch_state = get_json(f"{branch['url']}/api/state")
            main_state = mailbox_state()
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


@requires_statefork_integration
def test_commit_advances_statefork_head_without_sqlite_promotion(monkeypatch, tmp_path):
    app = load_controller_app(monkeypatch, tmp_path)
    branch = None
    next_branch = None
    with TestClient(app) as client:
        base = client.post("/api/bases", json={"label": "commit-base"}).json()["base"]
        branch = client.post(f"/api/bases/{base['id']}/branches").json()["branch"]
        try:
            agent = client.post(f"/api/branches/{branch['id']}/run-agent-demo")
            commit = client.post(f"/api/branches/{branch['id']}/commit")
            branches_after_commit = client.get("/api/branches").json()["branches"]
            next_branch = client.post("/api/branches").json()["branch"]
            committed_state = get_json(f"{next_branch['url']}/api/state")
            main_state = mailbox_state()
        finally:
            if next_branch:
                client.post(f"/api/branches/{next_branch['id']}/discard")
            elif branch:
                client.post(f"/api/branches/{branch['id']}/discard")

    assert agent.status_code == 200
    assert commit.status_code == 200
    commit_payload = commit.json()
    assert commit_payload["status"] == "committed"
    assert commit_payload["head_base"]["is_head"] is True
    assert commit_payload["head_base"]["checkpoint_id"] != base["checkpoint_id"]
    assert branches_after_commit == []

    committed_messages = {message["id"]: message for message in committed_state["messages"]}
    assert "finance" in committed_messages["msg-1001"]["labels"]
    assert committed_messages["msg-1003"]["folder"] == "Spam"
    assert committed_messages["msg-1004"]["folder"] == "Archive"
    assert committed_messages["msg-agent-2001"]["subject"] == "Follow-up: customer escalation"
    assert any(
        draft["source_message_id"] == "msg-1002"
        and draft["created_by"] == "agent"
        and "new ETA shortly" in draft["body"]
        for draft in committed_state["drafts"]
    )

    main_messages = {message["id"]: message for message in main_state["messages"]}
    assert "finance" not in main_messages["msg-1001"]["labels"]
    assert main_messages["msg-1003"]["folder"] == "Inbox"
    assert "msg-agent-2001" not in main_messages


@requires_statefork_integration
def test_commit_rejects_branch_when_statefork_head_changed_after_base(monkeypatch, tmp_path):
    app = load_controller_app(monkeypatch, tmp_path)
    branch = None
    with TestClient(app) as client:
        base = client.post("/api/bases", json={"label": "stale-base"}).json()["base"]
        branch = client.post(f"/api/bases/{base['id']}/branches").json()["branch"]
        new_head = client.post("/api/bases", json={"label": "new-head"}).json()["base"]
        agent = client.post(f"/api/branches/{branch['id']}/run-agent-demo")
        commit = client.post(f"/api/branches/{branch['id']}/commit")
        branches = client.get("/api/branches").json()["branches"]
        bases = client.get("/api/bases").json()["bases"]
        client.post(f"/api/branches/{branch['id']}/discard")

    assert new_head["is_head"] is True
    assert agent.status_code == 200
    assert commit.status_code == 400
    assert "Committed StateFork head changed after this branch base was created" in commit.json()["detail"]
    assert [active["id"] for active in branches] == [branch["id"]]
    assert [base["id"] for base in bases if base["is_head"]] == [new_head["id"]]


def test_statefork_backend_uses_manifest_runtime_command_and_env(monkeypatch, tmp_path):
    branch_dir = tmp_path / "branch"
    branch_dir.mkdir()
    calls = {}

    class FakeProcess:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(command, *, cwd, env, stdout, stderr):
        calls["command"] = command
        calls["cwd"] = cwd
        calls["env"] = env
        calls["stdout"] = stdout
        calls["stderr"] = stderr
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    backend = StateForkBackend(
        tmp_path,
        tmp_path / "demo.db",
        tmp_path,
        runtime_command="python -m uvicorn demo.app:app --host ${HOST} --port ${PORT}",
        runtime_cwd="${BRANCH_WORKDIR}",
        runtime_port_env="APP_PORT",
        state_files=["demo.db"],
        state_env={"DEMO_DB": "${BRANCH_WORKDIR}/demo.db"},
    )

    process = backend._start_branch_process(
        db_path=branch_dir / "demo.db",
        port=8765,
        cwd=branch_dir,
    )

    assert process.pid == 4321
    assert calls["command"] == [
        "python",
        "-m",
        "uvicorn",
        "demo.app:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8765",
    ]
    assert calls["cwd"] == branch_dir
    assert calls["env"]["APP_PORT"] == "8765"
    assert calls["env"]["DEMO_DB"] == str(branch_dir / "demo.db")
    assert calls["env"]["DEMO_MAILBOX_DB_PATH"] == str(branch_dir / "demo.db")
    assert calls["env"]["PYTHONPATH"].startswith(str(tmp_path / "src"))


def test_statefork_backend_diff_still_uses_primary_sqlite_state_file(tmp_path):
    base_db = tmp_path / "base.db"
    branch_db = tmp_path / "branch.db"
    for path, rows in [(base_db, ["seed"]), (branch_db, ["seed", "candidate"])]:
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE marker (id TEXT PRIMARY KEY)")
            conn.executemany("INSERT INTO marker(id) VALUES (?)", [(row,) for row in rows])

    backend = StateForkBackend(
        tmp_path,
        base_db,
        tmp_path,
        state_files=["branch.db"],
    )
    backend.bases["base-1"] = BaseHandle(
        id="base-1",
        backend="statefork",
        label="base",
        checkpoint_id="checkpoint-1",
        db_path=base_db,
        state_summary=backend._read_summary(base_db),
    )
    backend.branches["branch-1"] = BranchHandle(
        id="branch-1",
        backend="statefork",
        db_path=branch_db,
        port=8300,
        url="http://127.0.0.1:8300",
        base_id="base-1",
        work_dir=tmp_path,
    )

    diff = backend.diff("branch-1")

    assert diff["tables"] == ["marker"]
    assert diff["counts"]["marker"] == {"main": 1, "branch": 2, "delta": 1}
    assert backend.state_file_fingerprints(tmp_path)[0]["name"] == "branch.db"


def test_statefork_backend_rejects_concurrent_active_branch():
    backend = object.__new__(StateForkBackend)
    backend.name = "statefork"
    backend.branches = {
        "active-branch": BranchHandle(
            id="active-branch",
            backend="statefork",
            db_path=Path("branch.db"),
            port=8300,
            url="http://127.0.0.1:8300",
            status="running",
        )
    }

    with pytest.raises(BranchError, match="one active branch at a time"):
        StateForkBackend.create_branch(backend, base_id="base-1")


def test_statefork_command_errors_return_branch_error(tmp_path):
    backend = object.__new__(StateForkBackend)
    backend.statefork_cwd = tmp_path
    command_error = subprocess.CalledProcessError(
        returncode=1,
        cmd=["./checkpoint-lite", "build", "/tmp/demo"],
        stderr="bash_init binary not found",
    )

    with pytest.raises(BranchError, match="StateFork command failed") as excinfo:
        StateForkBackend._call_statefork(
            backend,
            lambda: (_ for _ in ()).throw(command_error),
        )

    assert "bash_init binary not found" in str(excinfo.value)


def test_statefork_build_base_reuses_initial_snapshot(tmp_path):
    db_path = tmp_path / "demo_mailbox.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE marker (id TEXT PRIMARY KEY)")

    class BuildManagerStub:
        session_id = "session-build"
        last_snapshot_id = "initial-build-snapshot"
        current_snapshot_id = "initial-build-snapshot"
        work_dir = str(tmp_path)

        def snapshot(self):
            raise AssertionError("build mode should reuse the manager's initial snapshot")

    manager = BuildManagerStub()
    backend = object.__new__(StateForkBackend)
    backend.name = "statefork"
    backend.project_root = tmp_path
    backend.main_db_path = db_path
    backend.statefork_kwargs = {"build": True}
    backend.bases = {}
    backend.base_managers = {}
    backend.branches = {}
    backend.operation_stats = {"snapshot": [], "restore": []}
    backend._create_statefork_manager = lambda: manager
    backend._cleanup_manager = lambda _manager: None

    base = StateForkBackend.create_base(backend, label="docker base")

    assert base["checkpoint_id"] == "initial-build-snapshot"
    assert base["session_id"] == "session-build"
    assert backend.base_managers[base["id"]] is manager


def test_statefork_status_reports_runtime_mode(tmp_path):
    backend = object.__new__(StateForkBackend)
    backend.name = "statefork"
    backend.statefork_root = tmp_path / "StateFork"
    backend.statefork_cwd = tmp_path / "StateFork"
    backend.statefork_method = "ckpt_build"
    backend.statefork_kwargs = {"build": True}
    backend.host = "127.0.0.1"
    backend.port_start = 8300
    backend.bases = {}
    backend.branches = {}
    backend.operation_stats = {"snapshot": [], "restore": []}

    status = StateForkBackend.status(backend)

    assert status["details"]["statefork_build"] is True
    assert status["details"]["statefork_runtime_mode"] == "docker-build"


@requires_statefork_integration
def test_reset_clears_bases_branches_and_mailbox_state(monkeypatch, tmp_path):
    app = load_controller_app(monkeypatch, tmp_path)
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
        state = mailbox_state()

    assert reset.json()["cleanup"] == {"branches_deleted": 1, "bases_deleted": 1}
    assert bases.json()["bases"] == []
    assert branches.json()["branches"] == []
    assert backend.json()["totals"] == {"bases": 0, "branches": 0, "snapshots": 0}
    assert backend.json()["operations"]["snapshot"]["count"] == 0
    assert backend.json()["operations"]["restore"]["count"] == 0
    assert len(state["messages"]) == 5
    assert len(state["drafts"]) == 1
    assert len(state["audit_log"]) == 1
