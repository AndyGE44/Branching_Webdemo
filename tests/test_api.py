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
from agent_safe_demo.control_plane.runtime_manager import CheckpointExecRuntimeManager, RuntimeProcessManager


def configure_env(monkeypatch, tmp_path, auth_password=None) -> None:
    db_path = tmp_path / "demo_mailbox.db"
    inventory_db_path = tmp_path / "demo_inventory.db"
    kv_db_path = tmp_path / "demo_kv.db"
    control_plane_db_path = tmp_path / "control_plane_metadata.db"
    monkeypatch.setenv("DEMO_MAILBOX_DB_PATH", str(db_path))
    monkeypatch.setenv("DEMO_INVENTORY_DB_PATH", str(inventory_db_path))
    monkeypatch.setenv("DEMO_KV_DB_PATH", str(kv_db_path))
    monkeypatch.setenv("DEMO_CONTROL_PLANE_DB_PATH", str(control_plane_db_path))
    monkeypatch.delenv("DEMO_APP_ID", raising=False)
    monkeypatch.setenv(
        "DEMO_STATEFORK_ROOT",
        os.getenv("DEMO_STATEFORK_ROOT", "/users/alexxjk/Andy_StateFork"),
    )
    monkeypatch.setenv(
        "DEMO_STATEFORK_CWD",
        os.getenv("DEMO_STATEFORK_CWD", "/users/alexxjk/Andy_StateFork"),
    )
    monkeypatch.setenv("DEMO_STATEFORK_METHOD", os.getenv("DEMO_STATEFORK_METHOD", "ckpt_build"))
    monkeypatch.setenv("DEMO_BRANCH_PORT_START", os.getenv("DEMO_BRANCH_PORT_START", "8300"))
    monkeypatch.delenv("DEMO_STATEFORK_BUILD", raising=False)
    monkeypatch.delenv("DEMO_MAILBOX_BRANCH_ID", raising=False)
    if auth_password is None:
        monkeypatch.delenv("DEMO_AUTH_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("DEMO_AUTH_PASSWORD", auth_password)


def load_controller_app(monkeypatch, tmp_path, auth_password=None, app_id=None):
    configure_env(monkeypatch, tmp_path, auth_password)
    if app_id is not None:
        monkeypatch.setenv("DEMO_APP_ID", app_id)
    for module_name in [
        "agent_safe_demo.control_plane.main",
        "agent_safe_demo.control_plane.app_registry",
        "agent_safe_demo.control_plane.manifest",
    ]:
        sys.modules.pop(module_name, None)
    module = importlib.import_module("agent_safe_demo.control_plane.main")
    module.reset_workspace_handles()
    return module.app


def get_json(url: str) -> dict:
    with urlrequest.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


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
        Path("src/agent_safe_demo/app_plane/shop_clothing/statefork.yaml")
    )

    assert manifest.id == "shop_clothing"
    assert manifest.name == "Clothing Shop"
    assert manifest.runtime.type == "checkpoint_exec"
    assert "run-shop.sh" in manifest.runtime.command
    assert manifest.runtime.cwd == "/app"
    assert manifest.runtime.port_env == "PORT"
    assert manifest.runtime.health_path == "/health"
    assert manifest.runtime.ui_path == "/"
    assert manifest.build is not None
    assert manifest.build.dockerfile_dir == "."
    assert manifest.state.files == []
    assert manifest.observability.state_summary_path == "/health"
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


def test_app_registry_discovers_manifests_and_reports_errors():
    from agent_safe_demo.control_plane import app_registry

    specs = app_registry.build_app_specs()
    assert set(specs) == {"shop_clothing", "shop_cookware", "shop_hardware"}
    for app_id, spec in specs.items():
        assert spec.db_backed is False
        assert spec.runtime_type == "checkpoint_exec"
        assert spec.manifest_path is not None
        assert spec.manifest_path.name == "statefork.yaml"
    assert app_registry.list_manifest_errors() == []


def test_controller_lists_and_switches_registered_apps(monkeypatch, tmp_path):
    app = load_controller_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        apps = client.get("/api/apps")
        selected = client.post("/api/apps/shop_cookware/select")
        backend = client.get("/api/backend")

    assert apps.status_code == 200
    payload = apps.json()
    assert payload["current_app_id"] == "shop_clothing"
    assert payload["manifest_errors"] == []
    assert {app["id"] for app in payload["apps"]} == {
        "shop_clothing",
        "shop_cookware",
        "shop_hardware",
    }
    clothing_app = next(app for app in payload["apps"] if app["id"] == "shop_clothing")
    assert clothing_app["manifest_loaded"] is True
    assert clothing_app["runtime_type"] == "checkpoint_exec"
    assert selected.status_code == 200
    assert selected.json()["current_app_id"] == "shop_cookware"
    backend_details = backend.json()["details"]
    assert backend_details["app_id"] == "shop_cookware"
    assert backend_details["manifest_path"].endswith("shop_cookware/statefork.yaml")
    assert backend_details["runtime_type"] == "checkpoint_exec"
    assert backend_details["runtime_port_env"] == "PORT"


def test_workspace_payload_uses_same_origin_runtime_proxy(monkeypatch, tmp_path):
    configure_env(monkeypatch, tmp_path)
    sys.modules.pop("agent_safe_demo.control_plane.main", None)
    module = importlib.import_module("agent_safe_demo.control_plane.main")
    branch = {"id": "branch-1", "url": "http://127.0.0.1:8300", "snapshots": []}

    workspace = module.workspace_payload(branch)["workspace"]

    assert workspace["runtime_url"] == "http://127.0.0.1:8300"
    assert workspace["runtime_proxy_url"] == "/runtime"
    assert workspace["runtime_ui_url"] == "/runtime/"
    # Website runtimes run with basename=/runtime: /runtime/* documents and root
    # static assets pass through, stray root-relative document links get the mount.
    assert module.runtime_forward_path("/runtime/api/state") == "/runtime/api/state"
    assert module.runtime_forward_path("/assets/app.js") == "/assets/app.js"
    assert module.runtime_forward_path("/collections/x") == "/runtime/collections/x"


def test_workspace_payload_includes_active_app_head(monkeypatch, tmp_path):
    configure_env(monkeypatch, tmp_path)
    sys.modules.pop("agent_safe_demo.control_plane.main", None)
    module = importlib.import_module("agent_safe_demo.control_plane.main")
    commit = module.commit_store.create_commit(
        app_id="shop_clothing",
        parent_commit_id=None,
        base_id="base-1",
        branch_id="branch-1",
        checkpoint_id="checkpoint-1",
        label="accepted cart",
        diff={"tables": ["cart"]},
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
    assert payload["commits"][0]["diff"] == {"tables": ["cart"]}

    module.branch_backend.head_base_id = "base-2"
    inactive_payload = module.workspace_payload(branch)
    assert inactive_payload["app_head"]["active"] is False
    assert inactive_payload["workspace"]["head_commit_id"] is None


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
def test_commit_endpoints_are_disabled(monkeypatch, tmp_path):
    app = load_controller_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        workspace_commit = client.post(
            "/api/workspace/commit",
            json={"label": "should not commit", "message": "nope"},
        )
        branch_commit = client.post("/api/branches/any-branch/commit")

    assert workspace_commit.status_code == 403
    assert workspace_commit.json()["detail"] == "Commit is disabled in this build."
    assert branch_commit.status_code == 403
    assert branch_commit.json()["detail"] == "Commit is disabled in this build."
def test_runtime_manager_builds_script_launcher_command(tmp_path):
    branch_dir = tmp_path / "branch"
    branch_dir.mkdir()
    manager = RuntimeProcessManager(
        project_root=tmp_path,
        host="127.0.0.1",
        app_uvicorn_target="unused:app",
        app_db_env_var="DEMO_KV_DB_PATH",
        runtime_command="bash ${PROJECT_ROOT}/run.sh --host ${HOST} --port ${PORT}",
        runtime_cwd="${BRANCH_WORKDIR}",
        runtime_port_env="PORT",
        state_env={"DEMO_KV_DB_PATH": "${BRANCH_WORKDIR}/demo_kv.db"},
    )

    launch = manager.build_launch(
        db_path=branch_dir / "demo_kv.db",
        port=8456,
        work_dir=branch_dir,
    )

    assert launch.command == [
        "bash",
        str(tmp_path / "run.sh"),
        "--host",
        "127.0.0.1",
        "--port",
        "8456",
    ]
    assert launch.cwd == branch_dir
    assert launch.env["PORT"] == "8456"
    assert launch.env["DEMO_KV_DB_PATH"] == str(branch_dir / "demo_kv.db")


def test_statefork_backend_uses_manifest_runtime_command_and_env(monkeypatch, tmp_path):
    branch_dir = tmp_path / "branch"
    branch_dir.mkdir()
    calls = {}

    class FakeProcess:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(command, *, cwd, env, stdout, stderr, start_new_session):
        calls["command"] = command
        calls["cwd"] = cwd
        calls["env"] = env
        calls["stdout"] = stdout
        calls["stderr"] = stderr
        calls["start_new_session"] = start_new_session
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
    assert calls["start_new_session"] is True


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
