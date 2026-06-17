from __future__ import annotations

import hashlib
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib import request
from urllib.error import HTTPError, URLError

from agent_safe_demo.control_plane.data_tier import DataTier, build_data_tier
from agent_safe_demo.control_plane.runtime_manager import CheckpointExecRuntimeManager, RuntimeProcessManager


class BranchError(RuntimeError):
    pass


class DirtyBranchError(BranchError):
    pass


def sqlite_fingerprint(db_path: Path) -> str:
    hasher = hashlib.sha256()
    with sqlite3.connect(db_path) as conn:
        for line in conn.iterdump():
            hasher.update(line.encode("utf-8"))
            hasher.update(b"\n")
    return hasher.hexdigest()


def path_fingerprint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False}
    hasher = hashlib.sha256()
    size = 0
    if path.is_dir():
        for child in sorted(item for item in path.rglob("*") if item.is_file()):
            relative = child.relative_to(path).as_posix()
            hasher.update(relative.encode("utf-8"))
            hasher.update(b"\0")
            data = child.read_bytes()
            hasher.update(data)
            size += len(data)
        kind = "directory"
    else:
        data = path.read_bytes()
        hasher.update(data)
        size = len(data)
        kind = "file"
    return {
        "path": str(path),
        "exists": True,
        "kind": kind,
        "sha256": hasher.hexdigest(),
        "size_bytes": size,
    }


def ensure_branch_base_is_current_head(
    *,
    head_base_id: str | None,
    branch: "BranchHandle",
) -> None:
    if head_base_id and branch.base_id != head_base_id:
        raise BranchError(
            "Committed StateFork head changed after this branch base was created. "
            "Discard this branch or create a new branch from the current head before committing."
        )


DEFAULT_APP_UVICORN_TARGET = "agent_safe_demo.app_plane.email_service.app:app"


def app_uvicorn_target() -> str:
    return os.getenv("DEMO_APP_UVICORN_TARGET", DEFAULT_APP_UVICORN_TARGET)


def new_operation_stats() -> dict[str, list[float]]:
    return {"snapshot": [], "restore": []}


def record_operation(stats: dict[str, list[float]], name: str, started_at: float) -> None:
    stats.setdefault(name, []).append(time.time() - started_at)


def summarize_operations(stats: dict[str, list[float]]) -> dict[str, dict[str, float | int]]:
    summary = {}
    for name, durations in stats.items():
        total = sum(durations)
        count = len(durations)
        summary[name] = {
            "count": count,
            "total_ms": round(total * 1000, 2),
            "mean_ms": round((total / count) * 1000, 2) if count else 0,
            "last_ms": round(durations[-1] * 1000, 2) if durations else 0,
        }
    return summary


def count_snapshots(branches: dict[str, "BranchHandle"]) -> int:
    return sum(len(branch.snapshots) for branch in branches.values())


def build_status(
    *,
    backend: str,
    method: str,
    host: str,
    port_start: int,
    bases: dict[str, "BaseHandle"],
    branches: dict[str, "BranchHandle"],
    operations: dict[str, list[float]],
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "backend": backend,
        "method": method,
        "host": host,
        "port_start": port_start,
        "totals": {
            "bases": len(bases),
            "branches": len(branches),
            "snapshots": count_snapshots(branches),
        },
        "operations": summarize_operations(operations),
        "details": details or {},
    }


def branch_action_request(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if "path" in payload:
        return payload["path"], payload.get("body", {})
    action = payload["action"]
    actor = payload.get("actor", "agent")
    if action == "label":
        return (
            f"/api/messages/{payload['message_id']}/label",
            {"label": payload["label"], "actor": actor},
        )
    if action == "move":
        return (
            f"/api/messages/{payload['message_id']}/move",
            {"folder": payload["folder"], "actor": actor},
        )
    if action == "draft":
        return (
            "/api/drafts",
            {
                "source_message_id": payload.get("source_message_id"),
                "to_address": payload["to_address"],
                "subject": payload["subject"],
                "body": payload["body"],
                "created_by": actor,
            },
        )
    if action == "receive":
        return (
            "/api/messages",
            {
                "id": payload.get("message_id"),
                "from_address": payload["from_address"],
                "to_address": payload["to_address"],
                "subject": payload["subject"],
                "body": payload["body"],
                "folder": payload.get("folder", "Inbox"),
                "is_read": payload.get("is_read", False),
                "priority": payload.get("priority", "normal"),
                "actor": actor,
            },
        )
    if action == "archive":
        return (
            f"/api/messages/{payload['message_id']}/archive",
            {"actor": actor},
        )
    raise BranchError(f"Unknown branch action: {action}")


AGENT_DEMO_ACTIONS = [
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


@dataclass
class BaseHandle:
    id: str
    backend: str
    label: str
    checkpoint_id: str
    session_id: str | None = None
    db_path: Path | None = None
    work_dir: Path | None = None
    status: str = "ready"
    main_fingerprint: str | None = None
    state_summary: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "backend": self.backend,
            "label": self.label,
            "checkpoint_id": self.checkpoint_id,
            "session_id": self.session_id,
            "db_path": str(self.db_path) if self.db_path else None,
            "work_dir": str(self.work_dir) if self.work_dir else None,
            "status": self.status,
            "main_fingerprint": self.main_fingerprint,
            "created_at": self.created_at,
        }


@dataclass
class SnapshotHandle:
    id: str
    backend: str
    label: str
    action: str
    parent_id: str | None
    db_path: Path | None = None
    fingerprint: str | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "backend": self.backend,
            "label": self.label,
            "action": self.action,
            "parent_id": self.parent_id,
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
        }


@dataclass
class BranchHandle:
    id: str
    backend: str
    db_path: Path
    port: int
    url: str
    base_id: str | None = None
    session_id: str | None = None
    base_checkpoint_id: str | None = None
    work_dir: Path | None = None
    status: str = "running"
    created_at: float = field(default_factory=time.time)
    snapshots: list[SnapshotHandle] = field(default_factory=list)
    current_snapshot_id: str | None = None
    last_saved_fingerprint: str | None = None
    dirty: bool = False
    process: subprocess.Popen | None = None
    runtime_type: str = "process"
    runtime_pid: int | None = None
    runtime_log_path: str | None = None
    checkpointing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "backend": self.backend,
            "db_path": str(self.db_path),
            "port": self.port,
            "url": self.url,
            "base_id": self.base_id,
            "session_id": self.session_id,
            "base_checkpoint_id": self.base_checkpoint_id,
            "work_dir": str(self.work_dir) if self.work_dir else None,
            "status": self.status,
            "created_at": self.created_at,
            "snapshots": [snapshot.to_dict() for snapshot in self.snapshots],
            "current_snapshot_id": self.current_snapshot_id,
            "dirty": self.dirty,
            "runtime_type": self.runtime_type,
            "runtime_pid": self.runtime_pid,
            "runtime_log_path": self.runtime_log_path,
            "checkpointing": self.checkpointing,
            "pid": self.runtime_pid if self.runtime_pid is not None else (self.process.pid if self.process else None),
        }


class StateForkBackend:
    """StateFork controller adapter for the base/branch demo API."""

    def __init__(
        self,
        project_root: Path,
        main_db_path: Path,
        statefork_root: Path,
        statefork_method: str = "ckpt_build",
        statefork_cwd: Path | None = None,
        statefork_kwargs: dict[str, Any] | None = None,
        host: str = "127.0.0.1",
        port_start: int = 8300,
        app_id: str = "email",
        app_label: str = "Email Service",
        app_uvicorn_target_value: str | None = None,
        app_db_env_var: str = "DEMO_MAILBOX_DB_PATH",
        health_path: str = "/api/state",
        runtime_command: str | None = None,
        runtime_cwd: str = ".",
        runtime_port_env: str = "PORT",
        runtime_type: str = "process",
        build_dockerfile_dir: Path | None = None,
        state_files: list[str] | tuple[str, ...] | None = None,
        state_env: dict[str, str] | None = None,
        manifest_path: Path | None = None,
        agent_demo_actions: list[dict[str, Any]] | None = None,
        data_backend: str = "sqlite",
        dolt_dir: Path | None = None,
        dolt_bin: str = "dolt",
    ) -> None:
        self.project_root = project_root
        self.main_db_path = main_db_path
        self.statefork_root = statefork_root
        self.statefork_method = statefork_method
        self.statefork_cwd = statefork_cwd or project_root
        self.statefork_kwargs = statefork_kwargs or {}
        self.host = host
        self.port_start = port_start
        self.app_id = app_id
        self.app_label = app_label
        self.app_uvicorn_target = app_uvicorn_target_value or app_uvicorn_target()
        self.app_db_env_var = app_db_env_var
        self.health_path = health_path
        self.runtime_command = runtime_command
        self.runtime_cwd = runtime_cwd
        self.runtime_port_env = runtime_port_env
        self.runtime_type = runtime_type
        self.build_dockerfile_dir = build_dockerfile_dir
        self.state_files = tuple(state_files or [main_db_path.name])
        self.state_env = dict(state_env or {})
        self.manifest_path = manifest_path
        runtime_manager_cls = (
            CheckpointExecRuntimeManager
            if runtime_type == "checkpoint_exec"
            else RuntimeProcessManager
        )
        self.runtime_manager = runtime_manager_cls(
            project_root=project_root,
            host=host,
            app_uvicorn_target=self.app_uvicorn_target,
            app_db_env_var=app_db_env_var,
            runtime_command=runtime_command,
            runtime_cwd=runtime_cwd,
            runtime_port_env=runtime_port_env,
            state_env=self.state_env,
        )
        self.agent_demo_actions = list(AGENT_DEMO_ACTIONS if agent_demo_actions is None else agent_demo_actions)
        self.name = "statefork"
        self.bases: dict[str, BaseHandle] = {}
        self.branches: dict[str, BranchHandle] = {}
        self.base_managers: dict[str, Any] = {}
        self.branch_environments: dict[str, str] = {}
        self.head_base_id: str | None = None
        self.operation_stats = new_operation_stats()

        # Optional external data tier (architecture A). None => the original
        # SQLite-file-in-checkpoint path (architecture B), unchanged.
        self.data_backend = data_backend
        self.data_tier: DataTier | None = build_data_tier(
            data_backend, dolt_dir=dolt_dir, dolt_bin=dolt_bin
        )

    def status(self) -> dict[str, Any]:
        return build_status(
            backend=self.name,
            method=f"statefork:{self.statefork_method}",
            host=self.host,
            port_start=self.port_start,
            bases=self.bases,
            branches=self.branches,
            operations=self.operation_stats,
            details={
                "app_id": getattr(self, "app_id", "email"),
                "app_label": getattr(self, "app_label", "Email Service"),
                "app_uvicorn_target": getattr(self, "app_uvicorn_target", app_uvicorn_target()),
                "app_db_env_var": getattr(self, "app_db_env_var", "DEMO_MAILBOX_DB_PATH"),
                "runtime_command": getattr(self, "runtime_command", None),
                "runtime_cwd": getattr(self, "runtime_cwd", "."),
                "runtime_port_env": getattr(self, "runtime_port_env", "PORT"),
                "runtime_type": getattr(self, "runtime_type", "process"),
                "build_dockerfile_dir": (
                    str(getattr(self, "build_dockerfile_dir", None))
                    if getattr(self, "build_dockerfile_dir", None)
                    else None
                ),
                "manifest_path": (
                    str(self.manifest_path) if getattr(self, "manifest_path", None) else None
                ),
                "state_files": self.state_file_fingerprints(),
                "head_base_id": getattr(self, "head_base_id", None),
                "statefork_root": str(self.statefork_root),
                "statefork_cwd": str(self.statefork_cwd),
                "statefork_method": self.statefork_method,
                "statefork_build": self._uses_statefork_build(),
                "statefork_runtime_mode": (
                    "docker-build" if self._uses_statefork_build() else "init"
                ),
            },
        )

    def list_bases(self) -> list[dict[str, Any]]:
        head_base_id = getattr(self, "head_base_id", None)
        bases = []
        for base in self.bases.values():
            payload = base.to_dict()
            payload["is_head"] = base.id == head_base_id
            bases.append(payload)
        return bases

    def create_base(self, label: str | None = None) -> dict[str, Any]:
        if self.data_tier is not None:
            # Architecture A: the data lives in the external Dolt repo, not a
            # SQLite file. Make sure the repo exists; the app seeds it.
            self.data_tier.prepare()
        elif not self.main_db_path.exists():
            raise BranchError(f"Main database does not exist: {self.main_db_path}")

        started_at = time.time()
        manager = self._create_statefork_manager()
        snapshot_id = self._initial_build_snapshot_id(manager)
        if not snapshot_id:
            snapshot_id = self._call_statefork(manager.snapshot)
        if not snapshot_id:
            self._cleanup_manager(manager)
            raise BranchError("StateFork snapshot failed")
        snapshot_id = str(snapshot_id)
        record_operation(self.operation_stats, "snapshot", started_at)

        base_id = f"sfbase-{snapshot_id}"
        work_dir = Path(getattr(manager, "work_dir", self.project_root))
        base_db_path = work_dir / self.main_db_path.name
        if not base_db_path.exists():
            base_db_path = self.main_db_path
        base = BaseHandle(
            id=base_id,
            backend="statefork",
            label=label or f"Base {len(self.bases) + 1}",
            checkpoint_id=snapshot_id,
            session_id=getattr(manager, "session_id", snapshot_id),
            db_path=base_db_path,
            work_dir=work_dir,
            main_fingerprint=self._data_fingerprint(base_db_path),
            state_summary=self._safe_data_summary(base_db_path),
        )
        self.bases[base_id] = base
        self.base_managers[base_id] = manager
        self.head_base_id = base_id
        payload = base.to_dict()
        payload["is_head"] = True
        return payload

    def delete_base(self, base_id: str) -> dict[str, Any]:
        self._require_base(base_id)
        active_branches = [
            branch.id
            for branch in self.branches.values()
            if branch.base_id == base_id and branch.status == "running"
        ]
        if active_branches:
            raise BranchError(
                f"Base {base_id} still has active branches: {', '.join(active_branches)}"
            )
        manager = self.base_managers.pop(base_id, None)
        if manager is not None:
            self._cleanup_manager(manager)
        self.bases.pop(base_id, None)
        if getattr(self, "head_base_id", None) == base_id:
            remaining_base_ids = list(self.bases)
            self.head_base_id = remaining_base_ids[-1] if remaining_base_ids else None
        return {"status": "deleted", "base_id": base_id}

    def create_branch(self, base_id: str | None = None) -> dict[str, Any]:
        self._ensure_single_active_branch()
        base = self._require_base(base_id) if base_id else self._current_head_base_or_auto()
        manager = self.base_managers.get(base.id)
        if manager is None:
            raise BranchError(f"Base {base.id} does not have a StateFork manager")

        restore_started_at = time.time()
        ok = self._call_statefork(lambda: manager.restore(base.checkpoint_id))
        if ok is False:
            raise BranchError(f"StateFork restore failed for base {base.id}")
        record_operation(self.operation_stats, "restore", restore_started_at)

        env_started_at = time.time()
        environment_name = self._call_statefork(
            lambda: manager.create_env_from_snapshot(base.checkpoint_id)
        )
        record_operation(self.operation_stats, "restore", env_started_at)
        if not environment_name:
            environment_name = base.checkpoint_id

        work_dir = Path(getattr(manager, "work_dir", base.work_dir or self.project_root))
        branch_db = work_dir / self.main_db_path.name

        branch_id = f"sf-{uuid.uuid4().hex[:8]}"
        port = self._next_port()
        process, runtime_pid, runtime_log_path = self._start_branch_runtime(
            manager=manager,
            branch_id=branch_id,
            db_path=branch_db,
            port=port,
            cwd=work_dir,
        )
        branch = BranchHandle(
            id=branch_id,
            backend="statefork",
            db_path=branch_db,
            port=port,
            url=f"http://{self.host}:{port}",
            base_id=base.id,
            session_id=str(environment_name),
            base_checkpoint_id=base.checkpoint_id,
            work_dir=work_dir,
            current_snapshot_id=base.checkpoint_id,
            process=process,
            runtime_type=self.runtime_type,
            runtime_pid=runtime_pid,
            runtime_log_path=runtime_log_path,
        )
        self.branches[branch_id] = branch
        self.branch_environments[branch_id] = str(environment_name)
        self._wait_until_ready(branch)
        if self.data_tier is None and not branch_db.exists():
            raise BranchError(f"App runtime did not create its database: {branch_db}")
        branch.last_saved_fingerprint = self._data_fingerprint(branch_db)
        branch.dirty = False
        return branch.to_dict()

    def list_branches(self) -> list[dict[str, Any]]:
        for branch in self.branches.values():
            self._refresh_status(branch)
        return [branch.to_dict() for branch in self.branches.values()]

    def apply_action(self, branch_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        branch = self._require_branch(branch_id)
        if branch.status != "running":
            raise BranchError(f"Branch {branch_id} is not running")

        path, action_payload = branch_action_request(payload)
        result = self._post_json(
            branch.url,
            path,
            action_payload,
        )
        branch.dirty = True
        return {
            "branch": branch.to_dict(),
            "action": result,
            "snapshot": None,
            "diff": self.diff(branch_id),
        }

    def run_agent_demo(self, branch_id: str) -> dict[str, Any]:
        if not self.agent_demo_actions:
            raise BranchError(f"App {self.app_id} does not define an agent demo")
        actions = []
        for payload in self.agent_demo_actions:
            result = self.apply_action(branch_id, payload)
            actions.append(result["action"])
        branch = self._require_branch(branch_id)
        return {
            "branch": branch.to_dict(),
            "actions": actions,
            "snapshots": [snapshot.to_dict() for snapshot in branch.snapshots],
            "diff": self.diff(branch_id),
        }

    def save_snapshot(self, branch_id: str, label: str | None = None) -> dict[str, Any]:
        branch = self._require_branch(branch_id)
        if branch.status != "running":
            raise BranchError(f"Branch {branch_id} is not running")
        snapshot = self._record_branch_snapshot(
            branch,
            "manual",
            label or f"Snapshot {len(branch.snapshots) + 1}",
        )
        return {"branch": branch.to_dict(), "snapshot": snapshot}

    def dirty(self, branch_id: str) -> dict[str, Any]:
        branch = self._require_branch(branch_id)
        return {
            "branch_id": branch_id,
            "dirty": self._branch_is_dirty(branch),
            "current_snapshot_id": branch.current_snapshot_id,
        }

    def restore_snapshot(
        self,
        branch_id: str,
        snapshot_id: str,
        force: bool = False,
    ) -> dict[str, Any]:
        branch = self._require_branch(branch_id)
        if branch.status != "running":
            raise BranchError(f"Branch {branch_id} is not running")
        if self._branch_is_dirty(branch) and not force:
            raise DirtyBranchError(
                "Branch has unsaved changes. Save a snapshot or discard changes before restoring."
            )
        snapshot = self._require_snapshot(branch, snapshot_id)
        self._restore_branch_snapshot(branch, snapshot)
        return {"branch": branch.to_dict(), "snapshot": snapshot.to_dict(), "status": "restored"}

    def diff(self, branch_id: str) -> dict[str, Any]:
        branch = self._require_branch(branch_id)
        main = self._summary_for_branch_base(branch)
        candidate = self._data_summary(branch.db_path)
        all_tables = sorted(set(main["counts"]) | set(candidate["counts"]))
        counts = {
            table: {
                "main": main["counts"].get(table, 0),
                "branch": candidate["counts"].get(table, 0),
                "delta": candidate["counts"].get(table, 0) - main["counts"].get(table, 0),
            }
            for table in all_tables
        }
        changed_tables = [
            table
            for table in all_tables
            if counts[table]["delta"]
            or main["fingerprints"].get(table) != candidate["fingerprints"].get(table)
        ]
        return {
            "branch_id": branch_id,
            "tables": changed_tables,
            "counts": counts,
        }

    def commit(self, branch_id: str) -> dict[str, Any]:
        branch = self._require_branch(branch_id)
        if branch.status != "running":
            raise BranchError(f"Branch {branch_id} is not running")
        if not branch.base_id:
            raise BranchError(f"Branch {branch_id} does not have a base")
        ensure_branch_base_is_current_head(
            head_base_id=getattr(self, "head_base_id", None),
            branch=branch,
        )
        base = self._require_base(branch.base_id)
        manager = self.base_managers.get(branch.base_id)
        if manager is None:
            raise BranchError(f"Base {branch.base_id} does not have a StateFork manager")

        if branch.runtime_type != "checkpoint_exec":
            self._terminate(branch)

        snapshot_started_at = time.time()
        with self._quiesce_branch(branch):
            snapshot_id = self._call_checkpoint_operation(branch, manager.snapshot, failure_value=None)
        if not snapshot_id:
            raise BranchError(f"StateFork commit snapshot failed for branch {branch_id}")
        snapshot_id = str(snapshot_id)
        record_operation(self.operation_stats, "snapshot", snapshot_started_at)

        restore_started_at = time.time()
        with self._quiesce_branch(branch):
            ok = self._call_checkpoint_operation(
                branch,
                lambda: manager.restore(snapshot_id),
                failure_value=False,
            )
        if ok is False:
            raise BranchError(f"StateFork restore failed for committed snapshot {snapshot_id}")
        record_operation(self.operation_stats, "restore", restore_started_at)

        if branch.runtime_type == "checkpoint_exec":
            self._wait_until_ready(branch)
            self._terminate(branch)

        fingerprint = self._data_fingerprint(branch.db_path)
        state_summary = self._data_summary(branch.db_path)
        base.checkpoint_id = snapshot_id
        base.label = f"Committed {branch.id}"
        base.session_id = getattr(manager, "session_id", base.session_id)
        base.db_path = branch.db_path
        base.work_dir = branch.work_dir
        base.main_fingerprint = fingerprint
        base.state_summary = state_summary
        self.head_base_id = base.id

        branch.status = "committed"
        branch.current_snapshot_id = snapshot_id
        branch.last_saved_fingerprint = fingerprint
        branch.dirty = False
        self._cleanup_session(branch_id)
        self.branches.pop(branch_id, None)

        head_base = base.to_dict()
        head_base["is_head"] = True
        return {"status": "committed", "branch": branch.to_dict(), "head_base": head_base}

    def discard(self, branch_id: str) -> dict[str, Any]:
        branch = self._require_branch(branch_id)
        branch.status = "discarded"
        self._terminate(branch)
        self._cleanup_session(branch_id)
        self.branches.pop(branch_id, None)
        return {"status": "discarded", "branch_id": branch_id}

    def reset(self) -> dict[str, Any]:
        branch_count = len(self.branches)
        base_count = len(self.bases)
        for branch in list(self.branches.values()):
            branch.status = "discarded"
            self._terminate(branch)
        for manager in list(self.base_managers.values()):
            self._cleanup_manager(manager)
        self.branches.clear()
        self.branch_environments.clear()
        self.bases.clear()
        self.base_managers.clear()
        self.head_base_id = None
        self.operation_stats = new_operation_stats()
        if self.data_tier is not None:
            self.data_tier.cleanup()
        return {"branches_deleted": branch_count, "bases_deleted": base_count}

    def _ensure_single_active_branch(self) -> None:
        for branch in self.branches.values():
            self._refresh_status(branch)
        active_branches = [
            branch.id
            for branch in self.branches.values()
            if branch.status == "running"
        ]
        if active_branches:
            raise BranchError(
                "StateFork backend supports one active branch at a time. "
                f"Commit or discard the existing branch first: {', '.join(active_branches)}"
            )

    def _uses_statefork_build(self) -> bool:
        if "build" in self.statefork_kwargs:
            return bool(self.statefork_kwargs["build"])
        return self.runtime_type == "checkpoint_exec"

    def _create_statefork_manager(self) -> Any:
        if not self.statefork_root.exists():
            raise BranchError(f"StateFork root does not exist: {self.statefork_root}")
        root = str(self.statefork_root)
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            from controller import create_env_manager
        except Exception as error:
            raise BranchError(f"Could not import StateFork controller: {error}") from error

        dockerfile_dir = self.build_dockerfile_dir or self.project_root
        kwargs = {
            "dockerfile_dir": str(dockerfile_dir),
            "build": self._uses_statefork_build(),
            **self.statefork_kwargs,
        }
        # Architecture A: make the StateFork manager version the external Dolt
        # data tier in lockstep with the app checkpoint (snapshot -> commit +
        # branch sf_<id>; restore -> reset working set to sf_<id>).
        if self.data_tier is not None:
            kwargs.update(self.data_tier.statefork_kwargs())
        if self.runtime_type == "checkpoint_exec" and not kwargs.get("build"):
            raise BranchError("checkpoint_exec runtime requires StateFork/checkpoint-lite build mode")
        return self._call_statefork(lambda: create_env_manager(self.statefork_method, **kwargs))

    def _initial_build_snapshot_id(self, manager: Any) -> str | None:
        if not self._uses_statefork_build():
            return None
        for attr in ("last_snapshot_id", "current_snapshot_id"):
            value = getattr(manager, attr, None)
            if value:
                return str(value)
        snapshot_graph = getattr(manager, "snapshot_graph", None)
        if isinstance(snapshot_graph, dict) and len(snapshot_graph) == 1:
            return str(next(iter(snapshot_graph)))
        return None

    def _record_branch_snapshot(
        self,
        branch: BranchHandle,
        action: str,
        label: str,
    ) -> dict[str, Any]:
        if not branch.base_id:
            raise BranchError(f"Branch {branch.id} does not have a base")
        manager = self.base_managers.get(branch.base_id)
        if manager is None:
            raise BranchError(f"Base {branch.base_id} does not have a StateFork manager")
        started_at = time.time()
        with self._quiesce_branch(branch):
            snapshot_id = self._call_checkpoint_operation(branch, manager.snapshot, failure_value=None)
        if not snapshot_id:
            raise BranchError(f"StateFork snapshot failed after {action}")
        if branch.runtime_type == "checkpoint_exec":
            self._wait_until_ready(branch)
        snapshot_id = str(snapshot_id)
        record_operation(self.operation_stats, "snapshot", started_at)
        parent_id = branch.current_snapshot_id or branch.base_checkpoint_id
        snapshot = SnapshotHandle(
            id=snapshot_id,
            backend="statefork",
            label=label,
            action=action,
            parent_id=parent_id,
            fingerprint=self._data_fingerprint(branch.db_path),
        )
        branch.snapshots.append(snapshot)
        branch.current_snapshot_id = snapshot.id
        branch.last_saved_fingerprint = snapshot.fingerprint
        branch.dirty = False
        return snapshot.to_dict()

    def _restore_branch_snapshot(self, branch: BranchHandle, snapshot: SnapshotHandle) -> None:
        if not branch.base_id:
            raise BranchError(f"Branch {branch.id} does not have a base")
        if branch.work_dir is None:
            raise BranchError(f"Branch {branch.id} does not have a work directory")
        manager = self.base_managers.get(branch.base_id)
        if manager is None:
            raise BranchError(f"Base {branch.base_id} does not have a StateFork manager")
        started_at = time.time()
        if branch.runtime_type == "checkpoint_exec":
            with self._quiesce_branch(branch):
                ok = self._call_checkpoint_operation(
                    branch,
                    lambda: manager.restore(snapshot.id),
                    failure_value=False,
                )
            if ok is False:
                raise BranchError(f"StateFork restore failed for snapshot {snapshot.id}")
            record_operation(self.operation_stats, "restore", started_at)
        else:
            self._terminate(branch)
            ok = self._call_statefork(lambda: manager.restore(snapshot.id))
            if ok is False:
                raise BranchError(f"StateFork restore failed for snapshot {snapshot.id}")
            record_operation(self.operation_stats, "restore", started_at)
            branch.process, _, _ = self._start_branch_runtime(
                manager=manager,
                branch_id=branch.id,
                db_path=branch.db_path,
                port=branch.port,
                cwd=branch.work_dir,
            )
        branch.status = "running"
        branch.current_snapshot_id = snapshot.id
        branch.last_saved_fingerprint = snapshot.fingerprint or sqlite_fingerprint(branch.db_path)
        branch.dirty = False
        self._wait_until_ready(branch)

    def _cleanup_session(self, branch_id: str) -> None:
        self.branch_environments.pop(branch_id, None)

    def _cleanup_manager(self, manager: Any) -> None:
        try:
            self._call_statefork(manager.cleanup)
        except Exception:
            pass

    def _call_checkpoint_operation(
        self,
        branch: BranchHandle,
        fn: Callable[[], Any],
        *,
        failure_value: Any,
    ) -> Any:
        attempts = 2 if branch.runtime_type == "checkpoint_exec" else 1
        result = failure_value
        for attempt in range(attempts):
            result = self._call_statefork(fn)
            if result != failure_value:
                return result
            if attempt + 1 < attempts:
                time.sleep(0.75)
        return result

    def _call_statefork(self, fn: Callable[[], Any]) -> Any:
        with self._statefork_working_directory():
            try:
                return fn()
            except BranchError:
                raise
            except subprocess.CalledProcessError as error:
                output = (error.stderr or error.stdout or str(error)).strip()
                command = " ".join(str(part) for part in error.cmd)
                raise BranchError(
                    f"StateFork command failed with exit {error.returncode}: {command}. {output}"
                ) from error
            except FileNotFoundError as error:
                missing = error.filename or str(error)
                raise BranchError(f"StateFork command not found: {missing}") from error

    @contextmanager
    def _statefork_working_directory(self) -> Iterator[None]:
        previous = Path.cwd()
        os.chdir(self.statefork_cwd)
        try:
            yield
        finally:
            os.chdir(previous)

    def _next_port(self) -> int:
        used = {branch.port for branch in self.branches.values()}
        port = self.port_start
        while port in used or not self._port_is_free(port):
            port += 1
        return port

    def _port_is_free(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            return sock.connect_ex((self.host, port)) != 0

    def _wait_until_ready(self, branch: BranchHandle) -> None:
        deadline = time.time() + 10
        while time.time() < deadline:
            self._refresh_status(branch)
            if branch.status == "exited":
                raise BranchError(f"Branch server exited early for {branch.id}")
            try:
                with request.urlopen(f"{branch.url}{self.health_path}", timeout=0.5) as response:
                    if response.status == 200:
                        return
            except URLError:
                time.sleep(0.2)
        raise BranchError(f"Timed out waiting for branch server {branch.id}")

    def _refresh_status(self, branch: BranchHandle) -> None:
        if branch.status in {"committed", "discarded"}:
            return
        if branch.runtime_type == "checkpoint_exec":
            manager = self._manager_for_branch(branch)
            if manager is not None and branch.runtime_pid is not None:
                is_running = self._call_statefork(
                    lambda: self.runtime_manager.is_running(manager=manager, pid=branch.runtime_pid)
                )
                if not is_running:
                    branch.status = "exited"
                    return
        elif branch.process and branch.process.poll() is not None:
            branch.status = "exited"
            return
        self._branch_is_dirty(branch)

    def _require_branch(self, branch_id: str) -> BranchHandle:
        branch = self.branches.get(branch_id)
        if branch is None:
            raise BranchError(f"Unknown branch: {branch_id}")
        self._refresh_status(branch)
        return branch

    def _require_snapshot(self, branch: BranchHandle, snapshot_id: str) -> SnapshotHandle:
        for snapshot in branch.snapshots:
            if snapshot.id == snapshot_id:
                return snapshot
        raise BranchError(f"Unknown snapshot for {branch.id}: {snapshot_id}")

    def _branch_is_dirty(self, branch: BranchHandle) -> bool:
        if self.data_tier is not None:
            if branch.last_saved_fingerprint:
                branch.dirty = self.data_tier.fingerprint() != branch.last_saved_fingerprint
            return branch.dirty
        if branch.last_saved_fingerprint and branch.db_path.exists():
            branch.dirty = sqlite_fingerprint(branch.db_path) != branch.last_saved_fingerprint
        return branch.dirty

    def _require_base(self, base_id: str) -> BaseHandle:
        base = self.bases.get(base_id)
        if base is None:
            raise BranchError(f"Unknown base checkpoint: {base_id}")
        return base

    def _current_head_base_or_auto(self) -> BaseHandle:
        head_base_id = getattr(self, "head_base_id", None)
        if head_base_id and head_base_id in self.bases:
            return self._require_base(head_base_id)
        return self._create_auto_base()

    def _create_auto_base(self) -> BaseHandle:
        base = self.create_base(label=f"Auto base {len(self.bases) + 1}")
        return self._require_base(base["id"])

    def _terminate(self, branch: BranchHandle) -> None:
        if branch.runtime_type == "checkpoint_exec":
            manager = self._manager_for_branch(branch)
            if manager is not None and branch.runtime_pid is not None:
                self._call_statefork(
                    lambda: self.runtime_manager.stop(manager=manager, pid=branch.runtime_pid)
                )
            return
        if branch.process and branch.process.poll() is None:
            self._signal_runtime(branch.process, signal.SIGTERM)
            try:
                branch.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._signal_runtime(branch.process, signal.SIGKILL)
                branch.process.wait(timeout=5)

    def _signal_runtime(self, process: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (AttributeError, ProcessLookupError, OSError):
            if sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()

    def _manager_for_branch(self, branch: BranchHandle) -> Any | None:
        if not branch.base_id:
            return None
        return self.base_managers.get(branch.base_id)

    @contextmanager
    def _quiesce_branch(self, branch: BranchHandle) -> Iterator[None]:
        if branch.runtime_type != "checkpoint_exec":
            yield
            return
        branch.checkpointing = True
        try:
            yield
        finally:
            branch.checkpointing = False

    def _start_branch_runtime(
        self,
        *,
        manager: Any,
        branch_id: str,
        db_path: Path,
        port: int,
        cwd: Path,
    ) -> tuple[subprocess.Popen | None, int | None, str | None]:
        if self.runtime_type == "checkpoint_exec":
            handle = self._call_statefork(
                lambda: self.runtime_manager.start(
                    manager=manager,
                    db_path=db_path,
                    port=port,
                    work_dir=cwd,
                    branch_id=branch_id,
                )
            )
            return None, handle.pid, handle.log_path
        process = self.runtime_manager.start(db_path=db_path, port=port, work_dir=cwd)
        return process, None, None

    def _start_branch_process(
        self,
        *,
        db_path: Path,
        port: int,
        cwd: Path,
    ) -> subprocess.Popen:
        process, _, _ = self._start_branch_runtime(
            manager=None,
            branch_id="manual",
            db_path=db_path,
            port=port,
            cwd=cwd,
        )
        if process is None:
            raise BranchError("checkpoint_exec runtime requires _start_branch_runtime")
        return process

    def state_file_fingerprints(self, work_dir: Path | None = None) -> list[dict[str, Any]]:
        root = work_dir or self._active_state_root()
        state_files = getattr(self, "state_files", None)
        if not state_files:
            main_db_path = getattr(self, "main_db_path", None)
            state_files = (Path(main_db_path).name,) if main_db_path else ()
        payload = []
        for state_file in state_files:
            path = Path(state_file)
            if not path.is_absolute():
                path = root / path
            fingerprint = path_fingerprint(path)
            fingerprint["name"] = state_file
            payload.append(fingerprint)
        return payload

    def _active_state_root(self) -> Path:
        for branch in getattr(self, "branches", {}).values():
            if branch.status == "running" and branch.work_dir is not None:
                return branch.work_dir
        return getattr(self, "project_root", Path("."))

    def _post_json(self, base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        import json

        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            body = error.read().decode("utf-8")
            try:
                detail = json.loads(body).get("detail", body)
            except json.JSONDecodeError:
                detail = body or error.reason
            raise BranchError(str(detail)) from error

    def _summary_for_branch_base(self, branch: BranchHandle) -> dict[str, Any]:
        if branch.base_id:
            base = self.bases.get(branch.base_id)
            if base and base.state_summary is not None:
                return base.state_summary
        summary = self._safe_data_summary(self.main_db_path)
        if summary is None:
            raise BranchError(f"Could not read base database summary for branch {branch.id}")
        return summary

    def _safe_read_summary(self, db_path: Path) -> dict[str, Any] | None:
        try:
            return self._read_summary(db_path)
        except sqlite3.Error:
            return None

    # ---- data-tier-aware fingerprint/summary ----------------------------- #
    # When self.data_tier is set (architecture A), data lives in an external
    # Dolt repo, not the SQLite file at db_path; route through the tier.
    def _data_fingerprint(self, db_path: Path) -> str:
        if self.data_tier is not None:
            return self.data_tier.fingerprint()
        return sqlite_fingerprint(db_path)

    def _data_summary(self, db_path: Path) -> dict[str, Any]:
        if self.data_tier is not None:
            return self.data_tier.summary()
        return self._read_summary(db_path)

    def _safe_data_summary(self, db_path: Path) -> dict[str, Any] | None:
        try:
            return self._data_summary(db_path)
        except (sqlite3.Error, RuntimeError):
            return None

    def _quote_identifier(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def _read_summary(self, db_path: Path) -> dict[str, Any]:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            table_names = [
                row["name"]
                for row in conn.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    ORDER BY name
                    """
                )
            ]
            counts: dict[str, int] = {}
            fingerprints: dict[str, str] = {}
            for table_name in table_names:
                quoted = self._quote_identifier(table_name)
                counts[table_name] = int(
                    conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
                )
                hasher = hashlib.sha256()
                for row in conn.execute(f"SELECT * FROM {quoted} ORDER BY rowid"):
                    hasher.update(repr(tuple(row)).encode("utf-8"))
                    hasher.update(b"\n")
                fingerprints[table_name] = hasher.hexdigest()
        return {"tables": table_names, "counts": counts, "fingerprints": fingerprints}
