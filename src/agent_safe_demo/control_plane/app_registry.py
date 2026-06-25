from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_safe_demo.control_plane.manifest import StateForkManifest, interpolate_template, load_manifest

APP_PLANE_DIR = Path(__file__).resolve().parents[1] / "app_plane"
# repo root: control_plane -> agent_safe_demo -> src -> <repo>
REPO_ROOT = Path(__file__).resolve().parents[3]
USER_SELECTABLE_APP_IDS = frozenset({"email", "inventory"})


@dataclass(frozen=True)
class AppSpec:
    id: str
    label: str
    description: str
    module: str
    uvicorn_target: str
    db_env_var: str
    db_filename: str
    db_path: Path
    project_root: Path
    init_db: Callable[[], None]
    health_path: str = "/api/state"
    state_path: str = "/api/state"
    runtime_ui_path: str = "/"
    agent_demo_label: str = "Run Agent"
    agent_demo_actions: Sequence[dict[str, Any]] | None = None
    manifest_path: Path | None = None
    runtime_command: str | None = None
    runtime_cwd: str = "."
    runtime_port_env: str = "PORT"
    runtime_type: str = "process"
    build_dockerfile_dir: Path | None = None
    state_files: tuple[str, ...] = field(default_factory=tuple)
    state_env: dict[str, str] = field(default_factory=dict)
    # False for manifest-only/container apps with no SQLite file (state captured
    # by the container checkpoint, e.g. the shopgym shops).
    db_backed: bool = True

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "uvicorn_target": self.uvicorn_target,
            "db_env_var": self.db_env_var,
            "db_filename": self.db_filename,
            "health_path": self.health_path,
            "state_path": self.state_path,
            "runtime_ui_path": self.runtime_ui_path,
            "agent_demo_label": self.agent_demo_label,
            "agent_demo_enabled": bool(self.agent_demo_actions),
            "manifest_loaded": self.manifest_path is not None,
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
            "runtime_command": self.runtime_command,
            "runtime_cwd": self.runtime_cwd,
            "runtime_port_env": self.runtime_port_env,
            "runtime_type": self.runtime_type,
            "build_dockerfile_dir": str(self.build_dockerfile_dir) if self.build_dockerfile_dir else None,
            "state_files": list(self.state_files),
            "state_env_keys": sorted(self.state_env),
            "db_backed": self.db_backed,
        }


@dataclass(frozen=True)
class PythonAppAdapter:
    module_name: str
    uvicorn_target: str
    db_env_var: str
    db_filename: str
    label: str
    description: str
    agent_demo_label: str
    agent_demo_actions: Sequence[dict[str, Any]] | None


EMAIL_AGENT_ACTIONS = [
    {
        "action": "label",
        "message_id": "msg-1001",
        "label": "finance",
        "actor": "agent",
        "snapshot_label": "label finance",
    },
    {
        "action": "move",
        "message_id": "msg-1003",
        "folder": "Spam",
        "actor": "agent",
        "snapshot_label": "move spam",
    },
    {
        "action": "draft",
        "source_message_id": "msg-1002",
        "to_address": "customer@acme.example",
        "subject": "Re: Urgent: shipment delay",
        "body": "Thanks for the update. We are checking the shipment and will send a new ETA shortly.",
        "actor": "agent",
        "snapshot_label": "draft reply",
    },
    {
        "action": "receive",
        "message_id": "msg-agent-2001",
        "from_address": "director@example.com",
        "to_address": "ops@example.com",
        "subject": "Follow-up: customer escalation",
        "body": "Please keep the shipment-delay customer updated and post the revised ETA in this thread.",
        "folder": "Inbox",
        "is_read": False,
        "priority": "high",
        "actor": "agent",
        "snapshot_label": "receive escalation",
    },
    {
        "action": "archive",
        "message_id": "msg-1004",
        "actor": "agent",
        "snapshot_label": "archive report",
    },
]


INVENTORY_AGENT_ACTIONS = [
    {
        "path": "/api/reservations",
        "body": {"part_id": "MCU-100", "quantity": 2, "actor": "agent"},
        "snapshot_label": "reserve control boards",
    },
    {
        "path": "/api/inventory/buy",
        "body": {"part_id": "SENSOR-9", "quantity": 6, "actor": "agent"},
        "snapshot_label": "reorder sensors",
    },
    {
        "path": "/api/inventory/sell",
        "body": {"part_id": "WIRE-RED", "quantity": 5, "actor": "agent"},
        "snapshot_label": "sell harness wire",
    },
]


APP_ADAPTERS = {
    "email": PythonAppAdapter(
        module_name="agent_safe_demo.app_plane.email_service.app",
        uvicorn_target="agent_safe_demo.app_plane.email_service.app:app",
        db_env_var="DEMO_MAILBOX_DB_PATH",
        db_filename="demo_mailbox.db",
        label="Email Service",
        description="Mailbox, labels, folders, and draft replies.",
        agent_demo_label="Run Email Agent",
        agent_demo_actions=EMAIL_AGENT_ACTIONS,
    ),
    "inventory": PythonAppAdapter(
        module_name="agent_safe_demo.app_plane.inventory_service.app",
        uvicorn_target="agent_safe_demo.app_plane.inventory_service.app:app",
        db_env_var="DEMO_INVENTORY_DB_PATH",
        db_filename="demo_inventory.db",
        label="Inventory Service",
        description="Parts, stock levels, reservations, and reorder actions.",
        agent_demo_label="Run Inventory Agent",
        agent_demo_actions=INVENTORY_AGENT_ACTIONS,
    ),
    "kv": PythonAppAdapter(
        module_name="agent_safe_demo.app_plane.kv_service.app",
        uvicorn_target="agent_safe_demo.app_plane.kv_service.app:app",
        db_env_var="DEMO_KV_DB_PATH",
        db_filename="demo_kv.db",
        label="KV Store",
        description="Tiny key-value service launched through a wrapper script.",
        agent_demo_label="Run Agent",
        agent_demo_actions=None,
    ),
}


def _module_spec(
    *,
    id: str,
    label: str,
    description: str,
    module_name: str,
    uvicorn_target: str,
    db_env_var: str,
    db_filename: str,
    agent_demo_label: str,
    agent_demo_actions: Sequence[dict[str, Any]] | None,
) -> AppSpec:
    module = importlib.import_module(module_name)
    return AppSpec(
        id=id,
        label=label,
        description=description,
        module=module_name,
        uvicorn_target=uvicorn_target,
        db_env_var=db_env_var,
        db_filename=db_filename,
        db_path=Path(module.DB_PATH),
        project_root=Path(module.PROJECT_ROOT),
        init_db=module.init_db,
        agent_demo_label=agent_demo_label,
        agent_demo_actions=agent_demo_actions,
        state_files=(db_filename,),
        state_env={db_env_var: f"${{BRANCH_WORKDIR}}/{db_filename}"},
    )


def _fallback_app_specs() -> dict[str, AppSpec]:
    specs = [
        _module_spec(
            id=app_id,
            label=adapter.label,
            description=adapter.description,
            module_name=adapter.module_name,
            uvicorn_target=adapter.uvicorn_target,
            db_env_var=adapter.db_env_var,
            db_filename=adapter.db_filename,
            agent_demo_label=adapter.agent_demo_label,
            agent_demo_actions=adapter.agent_demo_actions,
        )
        for app_id, adapter in APP_ADAPTERS.items()
        if app_id in USER_SELECTABLE_APP_IDS
    ]
    return {spec.id: spec for spec in specs}


def _primary_state_file(manifest: StateForkManifest, adapter: PythonAppAdapter) -> str:
    return manifest.state.files[0] if manifest.state.files else adapter.db_filename


def _primary_env_var(manifest: StateForkManifest, adapter: PythonAppAdapter) -> str:
    if manifest.state.env:
        return next(iter(manifest.state.env))
    return adapter.db_env_var


def _build_dockerfile_dir(path: Path, manifest: StateForkManifest, module: Any) -> Path | None:
    if manifest.build is None:
        return None
    project_root = Path(module.PROJECT_ROOT)
    raw = interpolate_template(
        manifest.build.dockerfile_dir,
        {
            "APP_DIR": str(path.parent),
            "PROJECT_ROOT": str(project_root),
        },
    )
    dockerfile_dir = Path(raw)
    if not dockerfile_dir.is_absolute():
        dockerfile_dir = path.parent / dockerfile_dir
    return dockerfile_dir.resolve()


def _container_spec_from_manifest(path: Path, manifest: StateForkManifest) -> AppSpec:
    """AppSpec for a manifest-only app with no Python module (e.g. a prebuilt
    container such as the shopgym shops). Snapshot/restore is handled entirely
    by the StateFork container checkpoint, so there is no SQLite file:
    ``db_backed=False`` and ``init_db`` is a no-op."""
    app_dir = path.parent
    db_env_var = next(iter(manifest.state.env), "") if manifest.state.env else ""
    # Sentinel path that never exists, so db `.exists()` checks cleanly skip.
    sentinel_db = REPO_ROOT / f".{manifest.id}-container-state"
    dockerfile_dir: Path | None = None
    if manifest.build is not None:
        raw = interpolate_template(
            manifest.build.dockerfile_dir,
            {"APP_DIR": str(app_dir), "PROJECT_ROOT": str(REPO_ROOT)},
        )
        dd = Path(raw)
        dockerfile_dir = (dd if dd.is_absolute() else app_dir / dd).resolve()
    return AppSpec(
        id=manifest.id,
        label=manifest.name,
        description=manifest.description,
        module="",
        uvicorn_target="",
        db_env_var=db_env_var,
        db_filename="",
        db_path=sentinel_db,
        project_root=REPO_ROOT,
        init_db=lambda: None,
        db_backed=False,
        health_path=manifest.runtime.health_path,
        state_path=manifest.observability.state_summary_path,
        runtime_ui_path=manifest.runtime.ui_path,
        agent_demo_label="Run",
        agent_demo_actions=None,
        manifest_path=path,
        runtime_command=manifest.runtime.command,
        runtime_cwd=manifest.runtime.cwd,
        runtime_port_env=manifest.runtime.port_env,
        runtime_type=manifest.runtime.type,
        build_dockerfile_dir=dockerfile_dir,
        state_files=tuple(manifest.state.files),
        state_env=dict(manifest.state.env),
    )


def _spec_from_manifest(path: Path, manifest: StateForkManifest) -> AppSpec:
    adapter = APP_ADAPTERS.get(manifest.id)
    if adapter is None:
        # No Python adapter. If the app declares no state file, treat it as an
        # external/container app whose state is captured by the checkpoint
        # (e.g. the shopgym shops). Otherwise it is a Python app missing its
        # adapter, which is a configuration error.
        if not manifest.state.files:
            return _container_spec_from_manifest(path, manifest)
        available = ", ".join(sorted(APP_ADAPTERS))
        raise ValueError(f"No Python adapter registered for app id {manifest.id!r}; available: {available}")

    module = importlib.import_module(adapter.module_name)
    db_env_var = _primary_env_var(manifest, adapter)
    db_filename = Path(_primary_state_file(manifest, adapter)).name
    return AppSpec(
        id=manifest.id,
        label=manifest.name,
        description=manifest.description,
        module=adapter.module_name,
        uvicorn_target=adapter.uvicorn_target,
        db_env_var=db_env_var,
        db_filename=db_filename,
        db_path=Path(module.DB_PATH),
        project_root=Path(module.PROJECT_ROOT),
        init_db=module.init_db,
        health_path=manifest.runtime.health_path,
        state_path=manifest.observability.state_summary_path,
        runtime_ui_path=manifest.runtime.ui_path,
        agent_demo_label=adapter.agent_demo_label,
        agent_demo_actions=adapter.agent_demo_actions,
        manifest_path=path,
        runtime_command=manifest.runtime.command,
        runtime_cwd=manifest.runtime.cwd,
        runtime_port_env=manifest.runtime.port_env,
        runtime_type=manifest.runtime.type,
        build_dockerfile_dir=_build_dockerfile_dir(path, manifest, module),
        state_files=tuple(manifest.state.files),
        state_env=dict(manifest.state.env),
    )


def _manifest_paths(app_plane_dir: Path) -> list[Path]:
    if not app_plane_dir.exists():
        return []
    return sorted(app_plane_dir.glob("*/statefork.yaml"))


def _manifest_specs(app_plane_dir: Path) -> tuple[dict[str, AppSpec], list[dict[str, str]]]:
    specs: dict[str, AppSpec] = {}
    errors: list[dict[str, str]] = []
    for path in _manifest_paths(app_plane_dir):
        try:
            manifest = load_manifest(path)
            if manifest.id in APP_ADAPTERS and manifest.id not in USER_SELECTABLE_APP_IDS:
                continue
            if manifest.id in specs:
                raise ValueError(f"Duplicate manifest id: {manifest.id}")
            specs[manifest.id] = _spec_from_manifest(path, manifest)
        except Exception as error:
            errors.append({"path": str(path), "error": str(error)})
    return specs, errors


def build_app_specs(app_plane_dir: Path | None = None) -> dict[str, AppSpec]:
    specs, _ = _manifest_specs(app_plane_dir or APP_PLANE_DIR)
    if specs:
        return specs
    return _fallback_app_specs()


def list_manifest_errors(app_plane_dir: Path | None = None) -> list[dict[str, str]]:
    _, errors = _manifest_specs(app_plane_dir or APP_PLANE_DIR)
    return errors


def list_app_specs() -> list[AppSpec]:
    return list(build_app_specs().values())


def get_app_spec(app_id: str | None = None) -> AppSpec:
    specs = build_app_specs()
    selected = app_id or os.getenv("DEMO_APP_ID", "email")
    try:
        return specs[selected]
    except KeyError as error:
        available = ", ".join(sorted(specs))
        raise ValueError(f"Unknown app id: {selected}. Available apps: {available}") from error
