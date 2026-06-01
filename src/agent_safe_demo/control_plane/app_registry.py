from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
        }


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
    )


def build_app_specs() -> dict[str, AppSpec]:
    specs = [
        _module_spec(
            id="email",
            label="Email Service",
            description="Mailbox, labels, folders, and draft replies.",
            module_name="agent_safe_demo.app_plane.email_service.app",
            uvicorn_target="agent_safe_demo.app_plane.email_service.app:app",
            db_env_var="DEMO_MAILBOX_DB_PATH",
            db_filename="demo_mailbox.db",
            agent_demo_label="Run Email Agent",
            agent_demo_actions=EMAIL_AGENT_ACTIONS,
        ),
        _module_spec(
            id="inventory",
            label="Inventory Service",
            description="Parts, stock levels, reservations, and reorder actions.",
            module_name="agent_safe_demo.app_plane.inventory_service.app",
            uvicorn_target="agent_safe_demo.app_plane.inventory_service.app:app",
            db_env_var="DEMO_INVENTORY_DB_PATH",
            db_filename="demo_inventory.db",
            agent_demo_label="Run Inventory Agent",
            agent_demo_actions=INVENTORY_AGENT_ACTIONS,
        ),
    ]
    return {spec.id: spec for spec in specs}


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
