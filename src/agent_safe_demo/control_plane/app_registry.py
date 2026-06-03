from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_safe_demo.control_plane.manifest import StateForkManifest, load_manifest

APP_PLANE_DIR = Path(__file__).resolve().parents[1] / "app_plane"


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
    state_files: tuple[str, ...] = field(default_factory=tuple)
    state_env: dict[str, str] = field(default_factory=dict)

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
            "state_files": list(self.state_files),
            "state_env_keys": sorted(self.state_env),
        }


@dataclass(frozen=True)
class PythonAppAdapter:
    module_name: str
    uvicorn_target: str
    db_env_var: str
    db_filename: str
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
        agent_demo_label="Run Email Agent",
        agent_demo_actions=EMAIL_AGENT_ACTIONS,
    ),
    "inventory": PythonAppAdapter(
        module_name="agent_safe_demo.app_plane.inventory_service.app",
        uvicorn_target="agent_safe_demo.app_plane.inventory_service.app:app",
        db_env_var="DEMO_INVENTORY_DB_PATH",
        db_filename="demo_inventory.db",
        agent_demo_label="Run Inventory Agent",
        agent_demo_actions=INVENTORY_AGENT_ACTIONS,
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
            id="email",
            label="Email Service",
            description="Mailbox, labels, folders, and draft replies.",
            module_name=APP_ADAPTERS["email"].module_name,
            uvicorn_target=APP_ADAPTERS["email"].uvicorn_target,
            db_env_var=APP_ADAPTERS["email"].db_env_var,
            db_filename=APP_ADAPTERS["email"].db_filename,
            agent_demo_label=APP_ADAPTERS["email"].agent_demo_label,
            agent_demo_actions=APP_ADAPTERS["email"].agent_demo_actions,
        ),
        _module_spec(
            id="inventory",
            label="Inventory Service",
            description="Parts, stock levels, reservations, and reorder actions.",
            module_name=APP_ADAPTERS["inventory"].module_name,
            uvicorn_target=APP_ADAPTERS["inventory"].uvicorn_target,
            db_env_var=APP_ADAPTERS["inventory"].db_env_var,
            db_filename=APP_ADAPTERS["inventory"].db_filename,
            agent_demo_label=APP_ADAPTERS["inventory"].agent_demo_label,
            agent_demo_actions=APP_ADAPTERS["inventory"].agent_demo_actions,
        ),
    ]
    return {spec.id: spec for spec in specs}


def _primary_state_file(manifest: StateForkManifest, adapter: PythonAppAdapter) -> str:
    return manifest.state.files[0] if manifest.state.files else adapter.db_filename


def _primary_env_var(manifest: StateForkManifest, adapter: PythonAppAdapter) -> str:
    if manifest.state.env:
        return next(iter(manifest.state.env))
    return adapter.db_env_var


def _spec_from_manifest(path: Path, manifest: StateForkManifest) -> AppSpec:
    adapter = APP_ADAPTERS.get(manifest.id)
    if adapter is None:
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
