import importlib
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_safe_demo.control_plane import app_registry
from agent_safe_demo.control_plane.app_registry import AppSpec
from agent_safe_demo.control_plane.manifest import interpolate_template, load_manifest
from agent_safe_demo.control_plane.proxy import runtime_forward_path
from agent_safe_demo.control_plane.runtime_manager import RuntimeManager
from agent_safe_demo.control_plane.statefork import (
    BranchError,
    BranchHandle,
    MergeConflictError,
    SnapshotHandle,
    StateForkBackend,
)
from agent_safe_demo.control_plane.idle import ActivityTracker, should_reset
from agent_safe_demo.control_plane.workspace import INITIAL_SNAPSHOT_LABEL, Workspace

SHOP_IDS = {"shop_clothing", "shop_cookware", "shop_hardware"}


def configure_env(monkeypatch, auth_password=None) -> None:
    monkeypatch.delenv("DEMO_APP_ID", raising=False)
    monkeypatch.delenv("DEMO_VISIBLE_APP_IDS", raising=False)
    monkeypatch.delenv("DEMO_STATEFORK_BUILD", raising=False)
    monkeypatch.delenv("DEMO_STATEFORK_KWARGS", raising=False)
    if auth_password is None:
        monkeypatch.delenv("DEMO_AUTH_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("DEMO_AUTH_PASSWORD", auth_password)


def load_controller_app(monkeypatch, auth_password=None, app_id=None):
    configure_env(monkeypatch, auth_password)
    if app_id is not None:
        monkeypatch.setenv("DEMO_APP_ID", app_id)
    # main builds its Workspace at import time from the environment.
    sys.modules.pop("agent_safe_demo.control_plane.main", None)
    module = importlib.import_module("agent_safe_demo.control_plane.main")
    return module.app


def make_app_spec(tmp_path) -> AppSpec:
    return AppSpec(
        id="shop_test",
        label="Test Shop",
        description="test",
        manifest_path=tmp_path / "statefork.yaml",
        dockerfile_dir=tmp_path,
        runtime_command="bash /app/run-shop.sh ${PORT}",
        runtime_cwd="/app",
        runtime_port_env="PORT",
        runtime_env={"UV_USE_IO_URING": "0"},
    )


def make_backend(tmp_path, **overrides) -> StateForkBackend:
    kwargs = {
        "statefork_root": tmp_path / "StateFork",
        "statefork_method": "ckpt_build",
        "statefork_kwargs": {"build": True},
        "host": "127.0.0.1",
        "port_start": 8300,
    }
    kwargs.update(overrides)
    return StateForkBackend(make_app_spec(tmp_path), **kwargs)


# --------------------------------------------------------------------- manifest


def test_statefork_manifest_loads_runtime_contract():
    manifest = load_manifest(
        Path("src/agent_safe_demo/app_plane/shop_clothing/statefork.yaml")
    )

    assert manifest.id == "shop_clothing"
    assert manifest.name == "Clothing Shop"
    assert "run-shop.sh" in manifest.runtime.command
    assert manifest.runtime.cwd == "/app"
    assert manifest.runtime.port_env == "PORT"
    assert manifest.runtime.health_path == "/health"
    assert manifest.runtime.ui_path == "/"
    assert manifest.runtime.env == {"UV_USE_IO_URING": "0"}
    assert manifest.build.dockerfile_dir == "."


def test_interpolate_template_keeps_unknown_placeholders():
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


# ------------------------------------------------------------------- app registry


def test_app_registry_discovers_shop_manifests(monkeypatch):
    configure_env(monkeypatch)
    specs = app_registry.build_app_specs()

    assert set(specs) == SHOP_IDS
    for spec in specs.values():
        assert spec.manifest_path.name == "statefork.yaml"
        assert spec.dockerfile_dir.is_absolute()
        assert "run-shop.sh" in spec.runtime_command
    assert app_registry.list_manifest_errors() == []


def test_app_registry_honors_visible_app_filter(monkeypatch):
    configure_env(monkeypatch)
    monkeypatch.setenv("DEMO_VISIBLE_APP_IDS", "shop_cookware")
    assert set(app_registry.build_app_specs()) == {"shop_cookware"}

    # A filter that would hide every app is ignored so the selector never
    # ends up empty.
    monkeypatch.setenv("DEMO_VISIBLE_APP_IDS", "not_a_real_app")
    assert set(app_registry.build_app_specs()) == SHOP_IDS


# --------------------------------------------------------------------- controller


def test_controller_lists_and_switches_registered_apps(monkeypatch):
    app = load_controller_app(monkeypatch)
    with TestClient(app) as client:
        apps = client.get("/api/apps")
        selected = client.post("/api/apps/shop_cookware/select")
        backend = client.get("/api/backend")
        unknown = client.post("/api/apps/not_an_app/select")

    assert apps.status_code == 200
    payload = apps.json()
    assert payload["current_app_id"] == "shop_clothing"
    assert payload["manifest_errors"] == []
    assert {entry["id"] for entry in payload["apps"]} == SHOP_IDS

    assert selected.status_code == 200
    assert selected.json()["current_app_id"] == "shop_cookware"
    details = backend.json()["details"]
    assert details["app_id"] == "shop_cookware"
    assert details["manifest_path"].endswith("shop_cookware/statefork.yaml")
    assert backend.json()["method"] == "statefork:ckpt_build"
    assert unknown.status_code == 404


def test_removed_endpoints_are_gone(monkeypatch):
    app = load_controller_app(monkeypatch)
    with TestClient(app) as client:
        # The commit feature and the raw base/branch API were removed; with no
        # branch running these must 404 rather than reach a storefront.
        workspace_commit = client.post("/api/workspace/commit")
        branch_commit = client.post("/api/branches/some-branch/commit")
        bases = client.get("/api/bases")

    assert workspace_commit.status_code == 404
    assert branch_commit.status_code == 404
    assert bases.status_code == 404


def test_workspace_payload_uses_same_origin_runtime_proxy(monkeypatch):
    configure_env(monkeypatch)
    sys.modules.pop("agent_safe_demo.control_plane.main", None)
    module = importlib.import_module("agent_safe_demo.control_plane.main")
    branch = {"id": "branch-1", "url": "http://127.0.0.1:8300", "snapshots": []}

    workspace = module.workspace.payload(branch)["workspace"]

    assert workspace["runtime_url"] == "http://127.0.0.1:8300"
    assert workspace["runtime_proxy_url"] == "/runtime"
    assert workspace["runtime_ui_url"] == "/runtime/"
    # The storefront runs with basename=/runtime: /runtime/* documents and root
    # static assets pass through, stray root-relative document links get the mount.
    assert runtime_forward_path("/runtime/api/state") == "/runtime/api/state"
    assert runtime_forward_path("/assets/app.js") == "/assets/app.js"
    assert runtime_forward_path("/collections/x") == "/runtime/collections/x"


def test_demo_password_protects_controller_app(monkeypatch):
    app = load_controller_app(monkeypatch, auth_password="secret-demo-password")
    with TestClient(app) as client:
        blocked = client.get("/api/backend")
        wrong_password = client.get("/api/backend", auth=("demo", "wrong"))
        allowed = client.get("/api/backend", auth=("demo", "secret-demo-password"))

    assert blocked.status_code == 401
    assert blocked.headers["www-authenticate"] == 'Basic realm="Agent-Safe Demo"'
    assert wrong_password.status_code == 401
    assert allowed.status_code == 200


# -------------------------------------------------------------------- workspace


class FakeBackend:
    """Records the calls Workspace makes; mimics the single-branch backend."""

    def __init__(self):
        self.calls = []
        self.active_base_id = None
        self._branches = []
        self._snap_count = 0

    def create_base(self, label=None):
        self.calls.append(("create_base", label))
        self.active_base_id = "base-1"
        return {"id": "base-1", "label": label}

    def create_branch(self, base_id):
        self.calls.append(("create_branch", base_id))
        branch = {
            "id": f"br-{len(self.calls)}",
            "url": "http://127.0.0.1:8300",
            "base_id": base_id,
            "status": "running",
            "snapshots": [],
            "current_snapshot_id": None,
        }
        self._branches = [branch]
        return dict(branch)

    def list_branches(self):
        return [dict(branch) for branch in self._branches]

    def save_snapshot(self, branch_id, label=None):
        self.calls.append(("save_snapshot", branch_id, label))
        self._snap_count += 1
        snapshot = {"id": f"snap-{self._snap_count}", "label": label}
        branch = self._branches[0]
        branch["snapshots"].append(snapshot)
        branch["current_snapshot_id"] = snapshot["id"]
        return {"branch": dict(branch), "snapshot": snapshot}

    def restore_snapshot(self, branch_id, snapshot_id):
        self.calls.append(("restore_snapshot", branch_id, snapshot_id))
        branch = self._branches[0]
        branch["current_snapshot_id"] = snapshot_id
        return {"branch": dict(branch), "snapshot": {"id": snapshot_id}, "status": "restored"}

    def merge_snapshots(self, branch_id, a_id, b_id, app_base_id, label=None, resolutions=None):
        self.calls.append(("merge_snapshots", branch_id, a_id, b_id, app_base_id, resolutions))
        return self.save_snapshot(branch_id, label=label or f"merged {a_id[:6]} + {b_id[:6]}")

    def reset(self):
        self.calls.append(("reset",))
        cleanup = {
            "branches_deleted": len(self._branches),
            "bases_deleted": 1 if self.active_base_id else 0,
        }
        self._branches = []
        self.active_base_id = None
        return cleanup


def make_workspace(tmp_path):
    workspace = Workspace(make_app_spec(tmp_path))
    workspace.backend = FakeBackend()
    return workspace


def test_workspace_ensure_builds_base_branch_and_initial_snapshot(tmp_path):
    workspace = make_workspace(tmp_path)

    payload = workspace.ensure()

    fake = workspace.backend
    assert [call[0] for call in fake.calls] == ["create_base", "create_branch", "save_snapshot"]
    assert fake.calls[1] == ("create_branch", "base-1")
    assert fake.calls[2][2] == INITIAL_SNAPSHOT_LABEL
    assert payload["workspace"]["runtime_ui_url"] == "/runtime/"
    assert payload["branch"]["snapshots"][0]["label"] == INITIAL_SNAPSHOT_LABEL

    # A second ensure() reuses the running branch — no new backend work.
    workspace.ensure()
    assert [call[0] for call in fake.calls] == ["create_base", "create_branch", "save_snapshot"]


def test_workspace_recovers_exited_branch_without_rebuilding_base(tmp_path):
    workspace = make_workspace(tmp_path)
    fake = workspace.backend
    workspace.ensure()
    fake.calls.clear()
    fake._branches[0]["status"] = "exited"  # the runtime died

    workspace.ensure()

    # The base (built image + checkpoint) is reused; only a branch is re-forked.
    assert [call[0] for call in fake.calls] == ["create_branch", "save_snapshot"]
    assert fake.calls[0] == ("create_branch", "base-1")


def test_workspace_snapshot_and_restore_delegate_to_ensured_branch(tmp_path):
    workspace = make_workspace(tmp_path)
    fake = workspace.backend

    result = workspace.snapshot(label="before agent")
    assert ("save_snapshot", result["branch"]["id"], "before agent") in fake.calls
    assert result["workspace"]["branch_id"] == result["branch"]["id"]

    restored = workspace.restore(result["snapshot"]["id"])
    assert restored["status"] == "restored"
    assert ("restore_snapshot", restored["branch"]["id"], result["snapshot"]["id"]) in fake.calls


def test_workspace_merge_translates_resolutions_to_tier_form(tmp_path):
    """The wire form (variant -> "a" | "b" | {field: value}) becomes the tier's
    take/set form keyed on the actual snapshot ids."""
    workspace = make_workspace(tmp_path)
    fake = workspace.backend
    workspace.ensure()  # snap-1 = Initial
    a_id = workspace.snapshot(label="A")["snapshot"]["id"]
    b_id = workspace.snapshot(label="B")["snapshot"]["id"]

    workspace.merge(
        a_id,
        b_id,
        app_base="a",
        resolutions={"10000": "a", "10001": "b", "10002": {"price": 6.5}},
    )
    call = fake.calls[-2]  # last is the save_snapshot the fake merge delegates to
    assert call[0] == "merge_snapshots"
    assert call[2] == a_id and call[3] == b_id and call[4] == a_id  # app base = A
    assert call[5] == {
        "10000": {"take": a_id},
        "10001": {"take": b_id},
        "10002": {"set": {"price": 6.5}},
    }

    # Without resolutions the backend receives None (first attempt).
    workspace.merge(a_id, b_id)
    assert fake.calls[-2][5] is None

    for bad in ("c", {}, 42):
        with pytest.raises(BranchError):
            workspace.merge(a_id, b_id, resolutions={"10000": bad})


def test_merge_endpoint_returns_409_with_conflicts_then_accepts_resolutions(monkeypatch):
    app = load_controller_app(monkeypatch)
    module = sys.modules["agent_safe_demo.control_plane.main"]
    conflicts = [
        {
            "variant_id": "10000",
            "product_title": "Hoodie",
            "variant_title": "XS",
            "fields": {"price": {"base": "61.99", "a": "5.00", "b": "7.77"}},
        }
    ]
    seen = {}

    def fake_merge(a, b, app_base="initial", resolutions=None):
        seen["resolutions"] = resolutions
        if resolutions is None:
            raise MergeConflictError(
                "Merge conflict on variant(s): 10000.",
                conflicts=conflicts,
                refs={"a": a, "b": b, "app_base": "init"},
            )
        return {"merged": True}

    monkeypatch.setattr(module.workspace, "merge", fake_merge)
    with TestClient(app) as client:
        first = client.post("/api/workspace/merge", json={"a": "s1", "b": "s2"})
        second = client.post(
            "/api/workspace/merge",
            json={"a": "s1", "b": "s2", "resolutions": {"10000": "b", "10001": {"price": "6.50"}}},
        )

    assert first.status_code == 409
    body = first.json()
    assert body["status"] == "conflict"
    assert body["conflicts"] == conflicts
    assert body["refs"] == {"a": "s1", "b": "s2", "app_base": "init"}

    assert second.status_code == 200
    assert second.json() == {"merged": True}
    assert seen["resolutions"] == {"10000": "b", "10001": {"price": "6.50"}}


def test_workspace_reset_rebuilds_from_scratch(tmp_path):
    workspace = make_workspace(tmp_path)
    fake = workspace.backend
    workspace.ensure()

    cleanup = workspace.reset()
    assert cleanup == {"branches_deleted": 1, "bases_deleted": 1}

    fake.calls.clear()
    workspace.ensure()
    # active_base_id was cleared, so a fresh base is built.
    assert [call[0] for call in fake.calls] == ["create_base", "create_branch", "save_snapshot"]


def test_workspace_select_app_resets_old_backend_and_switches(monkeypatch, tmp_path):
    configure_env(monkeypatch)
    monkeypatch.setenv("DEMO_APP_ID", "shop_clothing")
    workspace = Workspace()
    fake = FakeBackend()
    workspace.backend = fake

    result = workspace.select_app("shop_cookware")

    assert ("reset",) in fake.calls
    assert workspace.app.id == "shop_cookware"
    assert workspace.backend is not fake  # a fresh backend for the new app
    assert result["current_app_id"] == "shop_cookware"

    with pytest.raises(ValueError, match="Unknown app id"):
        workspace.select_app("not_an_app")


# ------------------------------------------------------------------- idle auto-reset


def test_activity_tracker_records_movement_and_dirtiness():
    tracker = ActivityTracker()
    assert tracker.dirty is False

    # A real request resets the idle clock; the liveness probe does not.
    tracker._last_activity -= 100
    tracker.record_request("/healthz")
    assert tracker.idle_seconds() > 50  # /healthz ignored — still idle
    tracker.record_request("/api/workspace")
    assert tracker.idle_seconds() < 5  # real movement reset the clock

    tracker.mark_dirty()
    assert tracker.dirty is True
    tracker.mark_clean()
    assert tracker.dirty is False


def test_idle_reset_only_fires_when_dirty_and_idle():
    tracker = ActivityTracker()
    window = 600.0

    # Fresh + clean: never reset, even after a long idle.
    tracker._last_activity -= 2 * window
    assert should_reset(tracker, window) is False

    # Dirty but recently active: not yet.
    tracker.mark_dirty()
    assert should_reset(tracker, window) is False

    # Dirty and idle past the window: reset.
    tracker._last_activity -= window + 60
    assert should_reset(tracker, window) is True

    # Back at the original clean state: idle no longer triggers a reset.
    tracker.mark_clean()
    tracker._last_activity -= 2 * window
    assert should_reset(tracker, window) is False


def test_healthz_is_open_and_not_counted_as_activity(monkeypatch):
    app = load_controller_app(monkeypatch, auth_password="secret-demo-password")
    import agent_safe_demo.control_plane.main as main

    with TestClient(app) as client:
        main.activity._last_activity -= 100  # pretend the demo has gone idle
        probe = client.get("/healthz")  # no credentials supplied
        assert probe.status_code == 200
        assert probe.json() == {"status": "ok"}
        # An authenticated request IS movement and resets the idle clock.
        assert client.get("/api/apps", auth=("demo", "secret-demo-password")).status_code == 200

    # /healthz must not have reset the clock; the /api/apps call must have.
    assert main.activity.idle_seconds() < 5


def test_idle_monitor_resets_dirty_workspace(monkeypatch):
    import asyncio

    from agent_safe_demo.control_plane.idle import run_idle_reset_monitor

    monkeypatch.setenv("DEMO_IDLE_RESET_MINUTES", "0.001")  # ~0.06s window
    monkeypatch.setenv("DEMO_IDLE_CHECK_SECONDS", "0.01")

    class FakeWorkspace:
        def __init__(self):
            self.calls = []

        def reset(self):
            self.calls.append("reset")
            return {}

        def ensure(self):
            self.calls.append("ensure")
            return {}

    workspace = FakeWorkspace()
    tracker = ActivityTracker()
    tracker.mark_dirty()
    tracker._last_activity -= 10  # well past the tiny idle window

    async def drive():
        task = asyncio.create_task(run_idle_reset_monitor(workspace, tracker))
        for _ in range(200):  # let the loop tick until it acts
            await asyncio.sleep(0.01)
            if workspace.calls:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())

    # The monitor ran the same reset the UI button does (tear down + rebuild)
    # and marked the workspace clean so it will not immediately reset again.
    assert workspace.calls[:2] == ["reset", "ensure"]
    assert tracker.dirty is False


def test_idle_monitor_can_be_disabled(monkeypatch):
    import asyncio

    from agent_safe_demo.control_plane.idle import run_idle_reset_monitor

    monkeypatch.setenv("DEMO_IDLE_RESET_MINUTES", "0")  # disabled

    class FakeWorkspace:
        def __init__(self):
            self.calls = []

        def reset(self):
            self.calls.append("reset")

        def ensure(self):
            self.calls.append("ensure")

    workspace = FakeWorkspace()
    tracker = ActivityTracker()
    tracker.mark_dirty()
    tracker._last_activity -= 10_000  # long idle — but disabled means no reset

    # Returns promptly instead of looping forever.
    asyncio.run(asyncio.wait_for(run_idle_reset_monitor(workspace, tracker), timeout=2))
    assert workspace.calls == []


# ----------------------------------------------------------------- statefork backend


def test_statefork_backend_rejects_concurrent_active_branch(tmp_path):
    backend = make_backend(tmp_path)
    backend.branches["active-branch"] = BranchHandle(
        id="active-branch",
        port=8300,
        url="http://127.0.0.1:8300",
        status="running",
    )

    with pytest.raises(BranchError, match="one active branch at a time"):
        backend.create_branch("base-1")


def test_statefork_command_errors_return_branch_error(tmp_path):
    import subprocess

    backend = make_backend(tmp_path, statefork_cwd=tmp_path)
    command_error = subprocess.CalledProcessError(
        returncode=1,
        cmd=["./checkpoint-lite", "build", "/tmp/demo"],
        stderr="bash_init binary not found",
    )

    with pytest.raises(BranchError, match="StateFork command failed") as excinfo:
        backend._call_statefork(lambda: (_ for _ in ()).throw(command_error))

    assert "bash_init binary not found" in str(excinfo.value)


def test_statefork_build_base_reuses_initial_snapshot(tmp_path, monkeypatch):
    backend = make_backend(tmp_path, statefork_cwd=tmp_path)

    class BuildManagerStub:
        session_id = "session-build"
        last_snapshot_id = "initial-build-snapshot"
        current_snapshot_id = "initial-build-snapshot"
        work_dir = str(tmp_path)

        def snapshot(self):
            raise AssertionError("build mode should reuse the manager's initial snapshot")

    manager = BuildManagerStub()
    monkeypatch.setattr(backend, "_create_statefork_manager", lambda: manager)

    base = backend.create_base(label="docker base")

    assert base["checkpoint_id"] == "initial-build-snapshot"
    assert base["session_id"] == "session-build"
    assert backend.base_managers[base["id"]] is manager
    assert backend.active_base_id == base["id"]


def test_statefork_calls_are_serialized_across_threads(tmp_path):
    backend = make_backend(tmp_path, statefork_cwd=tmp_path)
    active = 0
    max_active = 0
    counter_lock = threading.Lock()

    def operation():
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with counter_lock:
            active -= 1

    threads = [
        threading.Thread(target=lambda: backend._call_statefork(operation)) for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # The controller chdir()s process-wide, so calls must never overlap.
    assert max_active == 1


def test_save_snapshot_enforces_snapshot_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MAX_SNAPSHOTS", "3")
    backend = make_backend(tmp_path)
    backend.branches["br-1"] = BranchHandle(
        id="br-1",
        port=8300,
        url="http://127.0.0.1:8300",
        status="running",
        snapshots=[
            SnapshotHandle(id=f"snap-{i}", label="", action="manual", parent_id=None)
            for i in range(3)
        ],
    )

    with pytest.raises(BranchError, match="Snapshot limit reached"):
        backend.save_snapshot("br-1")


def test_statefork_status_reports_runtime_mode(tmp_path):
    backend = make_backend(tmp_path)

    status = backend.status()

    assert status["backend"] == "statefork"
    assert status["method"] == "statefork:ckpt_build"
    assert status["totals"] == {"bases": 0, "branches": 0, "snapshots": 0}
    assert status["details"]["statefork_build"] is True
    assert status["details"]["statefork_runtime_mode"] == "docker-build"
    assert status["details"]["app_id"] == "shop_test"


# ---------------------------------------------------------------- runtime manager


def test_runtime_manager_builds_checkpoint_launch(tmp_path):
    manager = RuntimeManager(
        host="127.0.0.1",
        command="bash /app/run-shop.sh ${PORT}",
        cwd="/app",
        port_env="PORT",
        env={"UV_USE_IO_URING": "0", "WORKDIR_HINT": "${BRANCH_WORKDIR}"},
    )

    launch = manager.build_launch(port=8456, work_dir=tmp_path)

    assert launch.command == ["bash", "/app/run-shop.sh", "8456"]
    assert launch.cwd == Path("/app")
    assert launch.env == {
        "PORT": "8456",
        "UV_USE_IO_URING": "0",
        "WORKDIR_HINT": str(tmp_path),
    }


def test_runtime_manager_start_launches_inside_managed_shell(tmp_path):
    calls = {}

    class FakeCheckpointManager:
        def exec_command(self, script, timeout):
            calls["script"] = script
            calls["timeout"] = timeout
            return 0, "RUNTIME_PID=4321\n", ""

    manager = RuntimeManager(
        host="127.0.0.1",
        command="bash /app/run-shop.sh ${PORT}",
        cwd="/app",
        port_env="PORT",
    )

    handle = manager.start(
        manager=FakeCheckpointManager(),
        port=8300,
        work_dir=tmp_path,
        branch_id="sf-test",
    )

    assert handle.pid == 4321
    assert handle.log_path == "/tmp/sf-test-runtime.log"
    assert calls["script"].startswith("cd /app && ")
    assert "PORT=8300" in calls["script"]
    assert "bash /app/run-shop.sh 8300" in calls["script"]


def test_runtime_manager_start_requires_pid(tmp_path):
    class BrokenCheckpointManager:
        def exec_command(self, script, timeout):
            return 0, "no pid here", ""

    manager = RuntimeManager(host="127.0.0.1", command="bash run.sh")

    with pytest.raises(RuntimeError, match="did not return a runtime PID"):
        manager.start(
            manager=BrokenCheckpointManager(),
            port=8300,
            work_dir=tmp_path,
            branch_id="sf-test",
        )
