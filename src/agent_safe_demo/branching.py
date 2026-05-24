from __future__ import annotations

import os
import shutil
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


class BranchError(RuntimeError):
    pass


def pythonpath_for(root: Path) -> str:
    src_path = str(root / "src")
    existing = os.environ.get("PYTHONPATH")
    if existing:
        return f"{src_path}{os.pathsep}{existing}"
    return src_path


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
    bases: dict[str, BaseHandle],
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


def branch_action_path(action: str) -> str:
    paths = {
        "buy": "/api/inventory/buy",
        "sell": "/api/inventory/sell",
        "reserve": "/api/reservations",
    }
    try:
        return paths[action]
    except KeyError as error:
        raise BranchError(f"Unknown branch action: {action}") from error


def branch_action_label(action: str, payload: dict[str, Any]) -> str:
    verb = {
        "buy": "Buy",
        "sell": "Sell",
        "reserve": "Reserve",
    }.get(action, action.title())
    return f"{verb} {payload['quantity']} {payload['part_id']}"


AGENT_DEMO_ACTIONS = [
    {"action": "sell", "part_id": "CASE-42", "quantity": 3, "actor": "agent"},
    {"action": "buy", "part_id": "SENSOR-9", "quantity": 5, "actor": "agent"},
    {"action": "reserve", "part_id": "MCU-100", "quantity": 2, "actor": "agent"},
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
            "created_at": self.created_at,
        }


@dataclass
class SnapshotHandle:
    id: str
    backend: str
    label: str
    action: str
    parent_id: str | None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "backend": self.backend,
            "label": self.label,
            "action": self.action,
            "parent_id": self.parent_id,
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
    process: subprocess.Popen | None = None

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
            "pid": self.process.pid if self.process else None,
        }


class LocalCopyBackend:
    """Local branch backend for development on macOS or non-CRIU hosts.

    Each branch gets a copy of the SQLite database and a separate uvicorn
    process. This is not isolation, but it matches the web workflow we need:
    independent URL, independent state, discard, and commit.
    """

    def __init__(
        self,
        project_root: Path,
        main_db_path: Path,
        host: str = "127.0.0.1",
        port_start: int = 8100,
    ) -> None:
        self.project_root = project_root
        self.main_db_path = main_db_path
        self.host = host
        self.port_start = port_start
        self.branches_dir = project_root / ".branches"
        self.bases_dir = self.branches_dir / "bases"
        self.bases: dict[str, BaseHandle] = {}
        self.branches: dict[str, BranchHandle] = {}
        self.operation_stats = new_operation_stats()
        self.name = "local-copy"

    def status(self) -> dict[str, Any]:
        return build_status(
            backend=self.name,
            method="file-copy",
            host=self.host,
            port_start=self.port_start,
            bases=self.bases,
            branches=self.branches,
            operations=self.operation_stats,
        )

    def list_bases(self) -> list[dict[str, Any]]:
        return [base.to_dict() for base in self.bases.values()]

    def create_base(self, label: str | None = None) -> dict[str, Any]:
        if not self.main_db_path.exists():
            raise BranchError(f"Main database does not exist: {self.main_db_path}")

        started_at = time.time()
        base_id = f"base-{uuid.uuid4().hex[:8]}"
        base_dir = self.bases_dir / base_id
        base_dir.mkdir(parents=True)
        base_db = base_dir / "toy_mailbox.db"
        shutil.copy2(self.main_db_path, base_db)
        record_operation(self.operation_stats, "snapshot", started_at)

        base = BaseHandle(
            id=base_id,
            backend="local-copy",
            label=label or f"Base {len(self.bases) + 1}",
            checkpoint_id=base_id,
            db_path=base_db,
            work_dir=base_dir,
        )
        self.bases[base_id] = base
        return base.to_dict()

    def delete_base(self, base_id: str) -> dict[str, Any]:
        base = self._require_base(base_id)
        active_branches = [
            branch.id
            for branch in self.branches.values()
            if branch.base_id == base_id and branch.status == "running"
        ]
        if active_branches:
            raise BranchError(
                f"Base {base_id} still has active branches: {', '.join(active_branches)}"
            )
        if base.work_dir:
            shutil.rmtree(base.work_dir, ignore_errors=True)
        self.bases.pop(base_id, None)
        return {"status": "deleted", "base_id": base_id}

    def create_branch(self, base_id: str | None = None) -> dict[str, Any]:
        base = self._require_base(base_id) if base_id else self._create_auto_base()

        started_at = time.time()
        branch_id = f"br-{uuid.uuid4().hex[:8]}"
        branch_dir = self.branches_dir / branch_id
        branch_dir.mkdir(parents=True)
        branch_db = branch_dir / "toy_mailbox.db"
        if base.db_path is None:
            raise BranchError(f"Base {base.id} does not have a database snapshot")
        shutil.copy2(base.db_path, branch_db)

        port = self._next_port()
        env = os.environ.copy()
        env["TOY_MAILBOX_DB_PATH"] = str(branch_db)
        env["TOY_MAILBOX_BRANCH_ID"] = branch_id
        env["PYTHONPATH"] = pythonpath_for(self.project_root)

        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "agent_safe_demo.main:app",
                "--host",
                self.host,
                "--port",
                str(port),
            ],
            cwd=self.project_root,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        handle = BranchHandle(
            id=branch_id,
            backend="local-copy",
            db_path=branch_db,
            port=port,
            url=f"http://{self.host}:{port}",
            base_id=base.id,
            base_checkpoint_id=base.checkpoint_id,
            process=process,
        )
        self.branches[branch_id] = handle
        self._wait_until_ready(handle)
        record_operation(self.operation_stats, "restore", started_at)
        return handle.to_dict()

    def list_branches(self) -> list[dict[str, Any]]:
        for branch in self.branches.values():
            self._refresh_status(branch)
        return [branch.to_dict() for branch in self.branches.values()]

    def apply_action(self, branch_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        branch = self._require_branch(branch_id)
        if branch.status != "running":
            raise BranchError(f"Branch {branch_id} is not running")

        action = payload["action"]
        action_payload = {
            "part_id": payload["part_id"],
            "quantity": payload["quantity"],
            "actor": payload.get("actor", "agent"),
        }
        result = self._post_json(
            branch.url,
            branch_action_path(action),
            action_payload,
        )
        snapshot = self._record_branch_snapshot(branch, action, branch_action_label(action, payload))
        return {
            "branch": branch.to_dict(),
            "action": result,
            "snapshot": snapshot,
            "diff": self.diff(branch_id),
        }

    def run_agent_demo(self, branch_id: str) -> dict[str, Any]:
        actions = []
        snapshots = []
        for payload in AGENT_DEMO_ACTIONS:
            result = self.apply_action(branch_id, payload)
            actions.append(result["action"])
            snapshots.append(result["snapshot"])
        branch = self._require_branch(branch_id)
        return {
            "branch": branch.to_dict(),
            "actions": actions,
            "snapshots": snapshots,
            "diff": self.diff(branch_id),
        }

    def diff(self, branch_id: str) -> dict[str, Any]:
        branch = self._require_branch(branch_id)
        main = self._read_summary(self.main_db_path)
        candidate = self._read_summary(branch.db_path)
        return {
            "branch_id": branch_id,
            "inventory": self._inventory_diff(main["inventory"], candidate["inventory"]),
            "counts": {
                table: {
                    "main": main["counts"][table],
                    "branch": candidate["counts"][table],
                    "delta": candidate["counts"][table] - main["counts"][table],
                }
                for table in main["counts"]
            },
        }

    def commit(self, branch_id: str) -> dict[str, Any]:
        branch = self._require_branch(branch_id)
        self._backup_sqlite(branch.db_path, self.main_db_path)
        branch.status = "committed"
        self._terminate(branch)
        self.branches.pop(branch_id, None)
        return {"status": "committed", "branch": branch.to_dict()}

    def discard(self, branch_id: str) -> dict[str, Any]:
        branch = self._require_branch(branch_id)
        branch.status = "discarded"
        self._terminate(branch)
        shutil.rmtree(branch.db_path.parent, ignore_errors=True)
        self.branches.pop(branch_id, None)
        return {"status": "discarded", "branch_id": branch_id}

    def reset(self) -> dict[str, Any]:
        branch_count = len(self.branches)
        base_count = len(self.bases)
        for branch in list(self.branches.values()):
            branch.status = "discarded"
            self._terminate(branch)
            shutil.rmtree(branch.db_path.parent, ignore_errors=True)
        self.branches.clear()
        shutil.rmtree(self.bases_dir, ignore_errors=True)
        self.bases.clear()
        self.operation_stats = new_operation_stats()
        return {"branches_deleted": branch_count, "bases_deleted": base_count}

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
                with request.urlopen(f"{branch.url}/api/state", timeout=0.5) as response:
                    if response.status == 200:
                        return
            except URLError:
                time.sleep(0.2)
        raise BranchError(f"Timed out waiting for branch server {branch.id}")

    def _refresh_status(self, branch: BranchHandle) -> None:
        if branch.status in {"committed", "discarded"}:
            return
        if branch.process and branch.process.poll() is not None:
            branch.status = "exited"

    def _require_branch(self, branch_id: str) -> BranchHandle:
        branch = self.branches.get(branch_id)
        if branch is None:
            raise BranchError(f"Unknown branch: {branch_id}")
        self._refresh_status(branch)
        return branch

    def _record_branch_snapshot(
        self,
        branch: BranchHandle,
        action: str,
        label: str,
    ) -> dict[str, Any]:
        started_at = time.time()
        parent_id = branch.snapshots[-1].id if branch.snapshots else branch.base_checkpoint_id
        snapshot = SnapshotHandle(
            id=f"logical-{uuid.uuid4().hex[:8]}",
            backend="local-copy",
            label=label,
            action=action,
            parent_id=parent_id,
        )
        branch.snapshots.append(snapshot)
        record_operation(self.operation_stats, "snapshot", started_at)
        return snapshot.to_dict()

    def _require_base(self, base_id: str) -> BaseHandle:
        base = self.bases.get(base_id)
        if base is None:
            raise BranchError(f"Unknown base checkpoint: {base_id}")
        return base

    def _create_auto_base(self) -> BaseHandle:
        base = self.create_base(label=f"Auto base {len(self.bases) + 1}")
        return self._require_base(base["id"])

    def _terminate(self, branch: BranchHandle) -> None:
        if branch.process and branch.process.poll() is None:
            branch.process.terminate()
            try:
                branch.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                branch.process.kill()
                branch.process.wait(timeout=5)

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

    def _read_summary(self, db_path: Path) -> dict[str, Any]:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            inventory = {
                row["id"]: dict(row)
                for row in conn.execute(
                    """
                    SELECT
                        p.id,
                        p.name,
                        p.on_hand,
                        p.on_hand - COALESCE(SUM(r.quantity), 0) AS available,
                        COALESCE(SUM(r.quantity), 0) AS reserved
                    FROM parts p
                    LEFT JOIN reservations r ON r.part_id = p.id AND r.status = 'active'
                    GROUP BY p.id
                    ORDER BY p.id
                    """
                )
            }
            counts = {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ["reservations", "audit_log"]
            }
        return {"inventory": inventory, "counts": counts}

    def _inventory_diff(
        self,
        main: dict[str, dict[str, Any]],
        candidate: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        changes = []
        for part_id, branch_item in candidate.items():
            main_item = main.get(part_id)
            if not main_item:
                continue
            on_hand_delta = branch_item["on_hand"] - main_item["on_hand"]
            available_delta = branch_item["available"] - main_item["available"]
            reserved_delta = branch_item["reserved"] - main_item["reserved"]
            if on_hand_delta or available_delta or reserved_delta:
                changes.append(
                    {
                        "part_id": part_id,
                        "on_hand_delta": on_hand_delta,
                        "available_delta": available_delta,
                        "reserved_delta": reserved_delta,
                    }
                )
        return changes

    def _backup_sqlite(self, source_path: Path, target_path: Path) -> None:
        source = sqlite3.connect(source_path)
        target = sqlite3.connect(target_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()


class CheckpointLiteBackend:
    """First Linux backend for checkpoint-lite branch servers.

    This is intentionally minimal: each branch is a checkpoint-lite session over
    the project root, and the branch app writes its SQLite DB inside the overlay
    workdir. It validates the branch web workflow on Ubuntu before we add deeper
    checkpoint/restore and multi-component service semantics.
    """

    def __init__(
        self,
        project_root: Path,
        main_db_path: Path,
        checkpoint_lite_bin: str = "./checkpoint-lite",
        checkpoint_sessions_dir: str = "/tmp/checkpoint-sessions",
        host: str = "127.0.0.1",
        port_start: int = 8200,
        use_sudo: bool = True,
    ) -> None:
        self.project_root = project_root
        self.main_db_path = main_db_path
        self.checkpoint_lite_bin = checkpoint_lite_bin
        self.checkpoint_sessions_dir = checkpoint_sessions_dir
        self.host = host
        self.port_start = port_start
        self.use_sudo = use_sudo
        self.name = "checkpoint-lite"
        self.bases: dict[str, BaseHandle] = {}
        self.branches: dict[str, BranchHandle] = {}
        self.sessions: dict[str, str] = {}
        self.operation_stats = new_operation_stats()

    def status(self) -> dict[str, Any]:
        return build_status(
            backend=self.name,
            method="checkpoint-lite-cli",
            host=self.host,
            port_start=self.port_start,
            bases=self.bases,
            branches=self.branches,
            operations=self.operation_stats,
            details={
                "checkpoint_lite_bin": self.checkpoint_lite_bin,
                "checkpoint_sessions_dir": self.checkpoint_sessions_dir,
            },
        )

    def list_bases(self) -> list[dict[str, Any]]:
        return [base.to_dict() for base in self.bases.values()]

    def create_base(self, label: str | None = None) -> dict[str, Any]:
        if not self.main_db_path.exists():
            raise BranchError(f"Main database does not exist: {self.main_db_path}")

        session_id, work_dir = self._checkpoint_lite_init(self.project_root)
        base_id = f"base-{session_id[:8]}"
        self._checkpoint_lite_create(session_id, base_id)
        base = BaseHandle(
            id=base_id,
            backend="checkpoint-lite",
            label=label or f"Base {len(self.bases) + 1}",
            checkpoint_id=base_id,
            session_id=session_id,
            db_path=Path(work_dir) / self.main_db_path.name,
            work_dir=Path(work_dir),
        )
        self.bases[base_id] = base
        return base.to_dict()

    def delete_base(self, base_id: str) -> dict[str, Any]:
        base = self._require_base(base_id)
        active_branches = [
            branch.id
            for branch in self.branches.values()
            if branch.base_id == base_id and branch.status == "running"
        ]
        if active_branches:
            raise BranchError(
                f"Base {base_id} still has active branches: {', '.join(active_branches)}"
            )
        if base.session_id:
            self._cleanup_session_by_id(base.session_id)
        self.bases.pop(base_id, None)
        return {"status": "deleted", "base_id": base_id}

    def create_branch(self, base_id: str | None = None) -> dict[str, Any]:
        base = self._require_base(base_id) if base_id else self._create_auto_base()
        if base.session_id is None or base.work_dir is None:
            raise BranchError(f"Base {base.id} does not have checkpoint-lite session data")

        self._checkpoint_lite_restore(base.session_id, base.checkpoint_id)
        session_id, work_dir = self._checkpoint_lite_init(base.work_dir)
        branch_id = f"ckpt-{session_id[:8]}"
        branch_start_checkpoint_id = f"{branch_id}-start"
        self._checkpoint_lite_create(session_id, branch_start_checkpoint_id)
        branch_db = Path(work_dir) / self.main_db_path.name
        port = self._next_port()

        env = os.environ.copy()
        env["TOY_MAILBOX_DB_PATH"] = str(branch_db)
        env["TOY_MAILBOX_BRANCH_ID"] = branch_id
        env["TOY_BRANCH_BACKEND"] = "local-copy"
        env["PYTHONPATH"] = pythonpath_for(Path(work_dir))

        command = [
            sys.executable,
            "-m",
            "uvicorn",
            "agent_safe_demo.main:app",
            "--host",
            self.host,
            "--port",
            str(port),
        ]
        if self.use_sudo and os.geteuid() != 0:
            command = [
                "sudo",
                "env",
                f"PYTHONPATH={env['PYTHONPATH']}",
                f"TOY_MAILBOX_DB_PATH={env['TOY_MAILBOX_DB_PATH']}",
                f"TOY_MAILBOX_BRANCH_ID={env['TOY_MAILBOX_BRANCH_ID']}",
                f"TOY_BRANCH_BACKEND={env['TOY_BRANCH_BACKEND']}",
                *command,
            ]

        process = subprocess.Popen(
            command,
            cwd=work_dir,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        branch = BranchHandle(
            id=branch_id,
            backend="checkpoint-lite",
            db_path=branch_db,
            port=port,
            url=f"http://{self.host}:{port}",
            base_id=base.id,
            session_id=session_id,
            base_checkpoint_id=base.checkpoint_id,
            work_dir=Path(work_dir),
            process=process,
        )
        self.branches[branch_id] = branch
        self.sessions[branch_id] = session_id
        self._wait_until_ready(branch)
        return branch.to_dict()

    def list_branches(self) -> list[dict[str, Any]]:
        for branch in self.branches.values():
            self._refresh_status(branch)
        return [branch.to_dict() for branch in self.branches.values()]

    def apply_action(self, branch_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        branch = self._require_branch(branch_id)
        if branch.status != "running":
            raise BranchError(f"Branch {branch_id} is not running")

        action = payload["action"]
        action_payload = {
            "part_id": payload["part_id"],
            "quantity": payload["quantity"],
            "actor": payload.get("actor", "agent"),
        }
        result = self._post_json(
            branch.url,
            branch_action_path(action),
            action_payload,
        )
        snapshot = self._record_branch_snapshot(branch, action, branch_action_label(action, payload))
        return {
            "branch": branch.to_dict(),
            "action": result,
            "snapshot": snapshot,
            "diff": self.diff(branch_id),
        }

    def run_agent_demo(self, branch_id: str) -> dict[str, Any]:
        actions = []
        snapshots = []
        for payload in AGENT_DEMO_ACTIONS:
            result = self.apply_action(branch_id, payload)
            actions.append(result["action"])
            snapshots.append(result["snapshot"])
        branch = self._require_branch(branch_id)
        return {
            "branch": branch.to_dict(),
            "actions": actions,
            "snapshots": snapshots,
            "diff": self.diff(branch_id),
        }

    def diff(self, branch_id: str) -> dict[str, Any]:
        branch = self._require_branch(branch_id)
        main = self._read_summary(self.main_db_path)
        candidate = self._read_summary(branch.db_path)
        return {
            "branch_id": branch_id,
            "inventory": self._inventory_diff(main["inventory"], candidate["inventory"]),
            "counts": {
                table: {
                    "main": main["counts"][table],
                    "branch": candidate["counts"][table],
                    "delta": candidate["counts"][table] - main["counts"][table],
                }
                for table in main["counts"]
            },
        }

    def commit(self, branch_id: str) -> dict[str, Any]:
        branch = self._require_branch(branch_id)
        self._backup_sqlite(branch.db_path, self.main_db_path)
        branch.status = "committed"
        self._terminate(branch)
        self._cleanup_session(branch_id)
        self.branches.pop(branch_id, None)
        return {"status": "committed", "branch": branch.to_dict()}

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
        for branch_id, branch in list(self.branches.items()):
            branch.status = "discarded"
            self._terminate(branch)
            self._cleanup_session(branch_id)
        for base in list(self.bases.values()):
            if base.session_id:
                self._cleanup_session_by_id(base.session_id)
        self.branches.clear()
        self.sessions.clear()
        self.bases.clear()
        self.operation_stats = new_operation_stats()
        return {"branches_deleted": branch_count, "bases_deleted": base_count}

    def _checkpoint_lite_init(self, work_directory: Path) -> tuple[str, str]:
        proc = subprocess.run(
            self._ckpt_cmd("init", str(work_directory), "--quiet"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise BranchError(
                "checkpoint-lite init failed: "
                f"{proc.stderr.strip() or proc.stdout.strip() or proc.returncode}"
            )
        output = proc.stdout.strip().splitlines()[-1]
        try:
            session_id, work_dir = output.split(",", 1)
        except ValueError as error:
            raise BranchError(f"Unexpected checkpoint-lite init output: {output}") from error
        return session_id, work_dir

    def _checkpoint_lite_create(self, session_id: str, checkpoint_id: str) -> None:
        started_at = time.time()
        proc = subprocess.run(
            self._ckpt_cmd("create", session_id, checkpoint_id, "-1"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            self._cleanup_session_by_id(session_id)
            raise BranchError(
                "checkpoint-lite base checkpoint failed: "
                f"{proc.stderr.strip() or proc.stdout.strip() or proc.returncode}"
            )
        record_operation(self.operation_stats, "snapshot", started_at)

    def _checkpoint_lite_restore(self, session_id: str, checkpoint_id: str) -> None:
        started_at = time.time()
        proc = subprocess.run(
            self._ckpt_cmd("restore", session_id, checkpoint_id),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise BranchError(
                "checkpoint-lite restore failed: "
                f"{proc.stderr.strip() or proc.stdout.strip() or proc.returncode}"
            )
        record_operation(self.operation_stats, "restore", started_at)

    def _cleanup_session(self, branch_id: str) -> None:
        session_id = self.sessions.pop(branch_id, None)
        if not session_id:
            return
        self._cleanup_session_by_id(session_id)

    def _cleanup_session_by_id(self, session_id: str) -> None:
        subprocess.run(
            self._ckpt_cmd("cleanup", session_id, "--force"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def _ckpt_cmd(self, *args: str) -> list[str]:
        cmd = [self.checkpoint_lite_bin, *args]
        env = [f"CHECKPOINT_SESSIONS_DIR={self.checkpoint_sessions_dir}"]
        if self.use_sudo and os.geteuid() != 0:
            return ["sudo", "env", *env, *cmd]
        return ["env", *env, *cmd]

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
                with request.urlopen(f"{branch.url}/api/state", timeout=0.5) as response:
                    if response.status == 200:
                        return
            except URLError:
                time.sleep(0.2)
        raise BranchError(f"Timed out waiting for branch server {branch.id}")

    def _refresh_status(self, branch: BranchHandle) -> None:
        if branch.status in {"committed", "discarded"}:
            return
        if branch.process and branch.process.poll() is not None:
            branch.status = "exited"

    def _require_branch(self, branch_id: str) -> BranchHandle:
        branch = self.branches.get(branch_id)
        if branch is None:
            raise BranchError(f"Unknown branch: {branch_id}")
        self._refresh_status(branch)
        return branch

    def _record_branch_snapshot(
        self,
        branch: BranchHandle,
        action: str,
        label: str,
    ) -> dict[str, Any]:
        if not branch.session_id:
            raise BranchError(f"Branch {branch.id} does not have a checkpoint-lite session")
        parent_id = branch.snapshots[-1].id if branch.snapshots else branch.base_checkpoint_id
        snapshot_id = f"{branch.id}-{len(branch.snapshots) + 1}-{action}"
        self._checkpoint_lite_create(branch.session_id, snapshot_id)
        snapshot = SnapshotHandle(
            id=snapshot_id,
            backend="checkpoint-lite",
            label=label,
            action=action,
            parent_id=parent_id,
        )
        branch.snapshots.append(snapshot)
        return snapshot.to_dict()

    def _require_base(self, base_id: str) -> BaseHandle:
        base = self.bases.get(base_id)
        if base is None:
            raise BranchError(f"Unknown base checkpoint: {base_id}")
        return base

    def _create_auto_base(self) -> BaseHandle:
        base = self.create_base(label=f"Auto base {len(self.bases) + 1}")
        return self._require_base(base["id"])

    def _terminate(self, branch: BranchHandle) -> None:
        if branch.process and branch.process.poll() is None:
            branch.process.terminate()
            try:
                branch.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                branch.process.kill()
                branch.process.wait(timeout=5)

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

    def _read_summary(self, db_path: Path) -> dict[str, Any]:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            inventory = {
                row["id"]: dict(row)
                for row in conn.execute(
                    """
                    SELECT
                        p.id,
                        p.name,
                        p.on_hand,
                        p.on_hand - COALESCE(SUM(r.quantity), 0) AS available,
                        COALESCE(SUM(r.quantity), 0) AS reserved
                    FROM parts p
                    LEFT JOIN reservations r ON r.part_id = p.id AND r.status = 'active'
                    GROUP BY p.id
                    ORDER BY p.id
                    """
                )
            }
            counts = {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ["reservations", "audit_log"]
            }
        return {"inventory": inventory, "counts": counts}

    def _inventory_diff(
        self,
        main: dict[str, dict[str, Any]],
        candidate: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        changes = []
        for part_id, branch_item in candidate.items():
            main_item = main.get(part_id)
            if not main_item:
                continue
            on_hand_delta = branch_item["on_hand"] - main_item["on_hand"]
            available_delta = branch_item["available"] - main_item["available"]
            reserved_delta = branch_item["reserved"] - main_item["reserved"]
            if on_hand_delta or available_delta or reserved_delta:
                changes.append(
                    {
                        "part_id": part_id,
                        "on_hand_delta": on_hand_delta,
                        "available_delta": available_delta,
                        "reserved_delta": reserved_delta,
                    }
                )
        return changes

    def _backup_sqlite(self, source_path: Path, target_path: Path) -> None:
        source = sqlite3.connect(source_path)
        target = sqlite3.connect(target_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()


class StateForkBackend(CheckpointLiteBackend):
    """StateFork controller adapter for the same base/branch demo API.

    StateFork is a Python controller package, not a web API in this workspace.
    This adapter uses its EnvironmentManager API as the control plane:
    snapshot, restore, create_env_from_snapshot, and cleanup. The toy web app
    still runs as uvicorn so the rest of the demo UI can stay unchanged.
    """

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
    ) -> None:
        super().__init__(
            project_root=project_root,
            main_db_path=main_db_path,
            checkpoint_lite_bin="",
            checkpoint_sessions_dir="",
            host=host,
            port_start=port_start,
            use_sudo=False,
        )
        self.name = "statefork"
        self.statefork_root = statefork_root
        self.statefork_method = statefork_method
        self.statefork_cwd = statefork_cwd or project_root
        self.statefork_kwargs = statefork_kwargs or {}
        self.base_managers: dict[str, Any] = {}
        self.branch_environments: dict[str, str] = {}

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
                "statefork_root": str(self.statefork_root),
                "statefork_cwd": str(self.statefork_cwd),
                "statefork_method": self.statefork_method,
            },
        )

    def create_base(self, label: str | None = None) -> dict[str, Any]:
        if not self.main_db_path.exists():
            raise BranchError(f"Main database does not exist: {self.main_db_path}")

        manager = self._create_statefork_manager()
        started_at = time.time()
        snapshot_id = self._call_statefork(manager.snapshot)
        if not snapshot_id:
            self._cleanup_manager(manager)
            raise BranchError("StateFork snapshot failed")
        record_operation(self.operation_stats, "snapshot", started_at)

        base_id = f"sfbase-{snapshot_id}"
        work_dir = Path(getattr(manager, "work_dir", self.project_root))
        base = BaseHandle(
            id=base_id,
            backend="statefork",
            label=label or f"Base {len(self.bases) + 1}",
            checkpoint_id=snapshot_id,
            session_id=getattr(manager, "current_snapshot", snapshot_id),
            db_path=work_dir / self.main_db_path.name,
            work_dir=work_dir,
        )
        self.bases[base_id] = base
        self.base_managers[base_id] = manager
        return base.to_dict()

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
        return {"status": "deleted", "base_id": base_id}

    def create_branch(self, base_id: str | None = None) -> dict[str, Any]:
        base = self._require_base(base_id) if base_id else self._create_auto_base()
        manager = self.base_managers.get(base.id)
        if manager is None:
            raise BranchError(f"Base {base.id} does not have a StateFork manager")

        restore_started_at = time.time()
        ok = self._call_statefork(lambda: manager.restore(base.checkpoint_id))
        if not ok:
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
        if not branch_db.exists():
            raise BranchError(f"StateFork branch database does not exist: {branch_db}")

        branch_id = f"sf-{uuid.uuid4().hex[:8]}"
        port = self._next_port()
        env = os.environ.copy()
        env["TOY_MAILBOX_DB_PATH"] = str(branch_db)
        env["TOY_MAILBOX_BRANCH_ID"] = branch_id
        env["TOY_BRANCH_BACKEND"] = "local-copy"
        env["PYTHONPATH"] = pythonpath_for(work_dir)

        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "agent_safe_demo.main:app",
                "--host",
                self.host,
                "--port",
                str(port),
            ],
            cwd=work_dir,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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
            process=process,
        )
        self.branches[branch_id] = branch
        self.branch_environments[branch_id] = str(environment_name)
        self._wait_until_ready(branch)
        return branch.to_dict()

    def discard(self, branch_id: str) -> dict[str, Any]:
        branch = self._require_branch(branch_id)
        branch.status = "discarded"
        self._terminate(branch)
        self.branch_environments.pop(branch_id, None)
        self.branches.pop(branch_id, None)
        return {"status": "discarded", "branch_id": branch_id}

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
        snapshot_id = self._call_statefork(manager.snapshot)
        if not snapshot_id:
            raise BranchError(f"StateFork snapshot failed after {action}")
        record_operation(self.operation_stats, "snapshot", started_at)
        parent_id = branch.snapshots[-1].id if branch.snapshots else branch.base_checkpoint_id
        snapshot = SnapshotHandle(
            id=snapshot_id,
            backend="statefork",
            label=label,
            action=action,
            parent_id=parent_id,
        )
        branch.snapshots.append(snapshot)
        return snapshot.to_dict()

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
        self.operation_stats = new_operation_stats()
        return {"branches_deleted": branch_count, "bases_deleted": base_count}

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

        kwargs = {
            "dockerfile_dir": str(self.project_root),
            "build": False,
            **self.statefork_kwargs,
        }
        return self._call_statefork(lambda: create_env_manager(self.statefork_method, **kwargs))

    def _cleanup_session(self, branch_id: str) -> None:
        self.branch_environments.pop(branch_id, None)

    def _cleanup_manager(self, manager: Any) -> None:
        try:
            self._call_statefork(manager.cleanup)
        except Exception:
            pass

    def _call_statefork(self, fn: Callable[[], Any]) -> Any:
        with self._statefork_working_directory():
            return fn()

    @contextmanager
    def _statefork_working_directory(self) -> Iterator[None]:
        previous = Path.cwd()
        os.chdir(self.statefork_cwd)
        try:
            yield
        finally:
            os.chdir(previous)
