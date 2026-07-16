"""StateFork backend: the base/branch/snapshot lifecycle behind the workspace.

Terminology (mirrors the UI):

- **base** — a built app image plus its initial checkpoint. Created once per
  workspace by StateFork's build mode (Waypoint ``buildah``-builds the app's
  Dockerfile and checkpoints the freshly started process tree).
- **branch** — a running fork of a base: StateFork restores the base
  checkpoint, forks an environment from it, and the runtime manager starts the
  app inside it. Only one branch runs at a time in this prototype.
- **snapshot** — a CRIU checkpoint of the branch's live process tree (cart
  included). Snapshots form a tree via ``parent_id``; restore rewinds the
  runtime to any of them.

All StateFork calls go through its Python controller API
(``controller.create_env_manager``), imported from ``DEMO_STATEFORK_ROOT``.
"""

from __future__ import annotations

import glob
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib import request
from urllib.error import URLError

from agent_safe_demo.control_plane.app_registry import REPO_ROOT, AppSpec
from agent_safe_demo.control_plane.runtime_manager import RuntimeManager

BACKEND_NAME = "statefork"

# Every StateFork call chdir()s into the controller directory — process-wide
# state — while API handlers run on a thread pool, so two concurrent requests
# (e.g. a snapshot racing a workspace refresh) could interleave chdir windows.
# One lock for the whole process: there is a single StateFork per host anyway.
_STATEFORK_LOCK = threading.RLock()


class BranchError(RuntimeError):
    pass


@dataclass
class BaseHandle:
    id: str
    label: str
    checkpoint_id: str
    session_id: str | None = None
    work_dir: Path | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "checkpoint_id": self.checkpoint_id,
            "session_id": self.session_id,
            "work_dir": str(self.work_dir) if self.work_dir else None,
            "created_at": self.created_at,
        }


@dataclass
class SnapshotHandle:
    id: str
    label: str
    action: str
    parent_id: str | None
    backend: str = BACKEND_NAME
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
    runtime_pid: int | None = None
    runtime_log_path: str | None = None
    # True while a CRIU snapshot/restore is in flight; the proxy answers 503
    # instead of racing the frozen runtime.
    checkpointing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
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
            "runtime_pid": self.runtime_pid,
            "runtime_log_path": self.runtime_log_path,
            "checkpointing": self.checkpointing,
        }


def _summarize_operations(stats: dict[str, list[float]]) -> dict[str, dict[str, float | int]]:
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


class StateForkBackend:
    """StateFork controller adapter for the workspace lifecycle."""

    def __init__(
        self,
        app: AppSpec,
        *,
        statefork_root: Path,
        statefork_method: str = "ckpt_build",
        statefork_cwd: Path | None = None,
        statefork_kwargs: dict[str, Any] | None = None,
        host: str = "127.0.0.1",
        port_start: int = 8300,
    ) -> None:
        self.app = app
        self.statefork_root = Path(statefork_root)
        self.statefork_method = statefork_method
        self.statefork_cwd = Path(statefork_cwd) if statefork_cwd else self.statefork_root
        self.statefork_kwargs = dict(statefork_kwargs or {})
        self.host = host
        self.port_start = port_start
        self.runtime_manager = RuntimeManager(
            host=host,
            command=app.runtime_command,
            cwd=app.runtime_cwd,
            port_env=app.runtime_port_env,
            env=app.runtime_env,
        )
        self.name = BACKEND_NAME
        self.bases: dict[str, BaseHandle] = {}
        self.branches: dict[str, BranchHandle] = {}
        self.base_managers: dict[str, Any] = {}
        # The base the workspace runs on. If the branch dies, a new one is
        # forked from this base without rebuilding the image.
        self.active_base_id: str | None = None
        self.operation_stats: dict[str, list[float]] = {"snapshot": [], "restore": []}
        # Optional external Dolt data tier (architecture A). When set, the
        # storefront's pricing/inventory lives outside the checkpoint and is
        # versioned by Dolt branches in lockstep with the CRIU snapshot/restore
        # below. None keeps the original in-checkpoint behaviour (zero change).
        self.data_tier: Any | None = None

    def set_data_tier(self, tier: Any, runtime_env: dict[str, str] | None = None) -> None:
        """Attach an external data tier and inject its connection env into the
        shop runtime (so the in-runtime mock API reaches the same Dolt db)."""
        self.data_tier = tier
        if runtime_env:
            self.runtime_manager.env.update(runtime_env)

    def _data_tier_snapshot(self, snapshot_id: str) -> None:
        if self.data_tier is None:
            return
        try:
            self.data_tier.on_snapshot(str(snapshot_id))
        except Exception as error:
            raise BranchError(
                f"Dolt data-tier snapshot failed for {snapshot_id}: {error}"
            ) from error

    def _data_tier_restore(self, snapshot_id: str) -> None:
        if self.data_tier is None:
            return
        try:
            self.data_tier.on_restore(str(snapshot_id))
        except Exception as error:
            raise BranchError(
                f"Dolt data-tier restore failed for {snapshot_id}: {error}"
            ) from error

    def _data_tier_cleanup(self) -> None:
        if self.data_tier is None:
            return
        try:
            self.data_tier.cleanup()
        except Exception:
            pass

    @classmethod
    def from_env(cls, app: AppSpec) -> "StateForkBackend":
        """Build a backend from the DEMO_* environment (see the launchers)."""
        statefork_root = Path(
            os.getenv("DEMO_STATEFORK_ROOT", str(REPO_ROOT.parent / "StateFork"))
        )
        kwargs = json.loads(os.getenv("DEMO_STATEFORK_KWARGS", "{}"))
        if "build" not in kwargs and "DEMO_STATEFORK_BUILD" in os.environ:
            kwargs["build"] = os.getenv("DEMO_STATEFORK_BUILD", "1") != "0"
        return cls(
            app,
            statefork_root=statefork_root,
            statefork_method=os.getenv("DEMO_STATEFORK_METHOD", "ckpt_build"),
            statefork_cwd=Path(os.getenv("DEMO_STATEFORK_CWD", str(statefork_root))),
            statefork_kwargs=kwargs,
            host=os.getenv("DEMO_BRANCH_HOST", "127.0.0.1"),
            port_start=int(os.getenv("DEMO_BRANCH_PORT_START", "8300")),
        )

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "method": f"statefork:{self.statefork_method}",
            "host": self.host,
            "port_start": self.port_start,
            "totals": {
                "bases": len(self.bases),
                "branches": len(self.branches),
                "snapshots": sum(len(branch.snapshots) for branch in self.branches.values()),
            },
            "operations": _summarize_operations(self.operation_stats),
            "details": {
                "app_id": self.app.id,
                "app_label": self.app.label,
                "runtime_command": self.app.runtime_command,
                "dockerfile_dir": str(self.app.dockerfile_dir),
                "manifest_path": str(self.app.manifest_path),
                "active_base_id": self.active_base_id,
                "statefork_root": str(self.statefork_root),
                "statefork_cwd": str(self.statefork_cwd),
                "statefork_method": self.statefork_method,
                "statefork_build": self._uses_statefork_build(),
                "statefork_runtime_mode": (
                    "docker-build" if self._uses_statefork_build() else "init"
                ),
            },
        }

    # --------------------------------------------------------------- lifecycle

    def create_base(self, label: str | None = None) -> dict[str, Any]:
        started_at = time.time()
        manager = self._create_statefork_manager()
        # Build mode already checkpointed the freshly built app; reuse that
        # snapshot instead of taking a redundant one.
        snapshot_id = self._initial_build_snapshot_id(manager)
        if not snapshot_id:
            snapshot_id = self._call_statefork(manager.snapshot)
        if not snapshot_id:
            self._cleanup_manager(manager)
            raise BranchError("StateFork snapshot failed")
        snapshot_id = str(snapshot_id)
        self._record_operation("snapshot", started_at)
        # Pin the seeded catalog to a Dolt branch matching this base checkpoint.
        self._data_tier_snapshot(snapshot_id)

        base = BaseHandle(
            id=f"sfbase-{snapshot_id}",
            label=label or f"Base {len(self.bases) + 1}",
            checkpoint_id=snapshot_id,
            session_id=getattr(manager, "session_id", snapshot_id),
            work_dir=Path(getattr(manager, "work_dir", REPO_ROOT)),
        )
        self.bases[base.id] = base
        self.base_managers[base.id] = manager
        self.active_base_id = base.id
        return base.to_dict()

    def create_branch(self, base_id: str) -> dict[str, Any]:
        self._ensure_single_active_branch()
        base = self._require_base(base_id)
        manager = self._require_manager(base.id)

        restore_started_at = time.time()
        ok = self._call_statefork(lambda: manager.restore(base.checkpoint_id))
        if ok is False:
            raise BranchError(f"StateFork restore failed for base {base.id}")
        self._record_operation("restore", restore_started_at)

        env_started_at = time.time()
        environment_name = self._call_statefork(
            lambda: manager.create_env_from_snapshot(base.checkpoint_id)
        )
        self._record_operation("restore", env_started_at)
        if not environment_name:
            environment_name = base.checkpoint_id

        work_dir = Path(getattr(manager, "work_dir", base.work_dir or REPO_ROOT))
        branch_id = f"sf-{uuid.uuid4().hex[:8]}"
        port = self._next_port()
        handle = self._call_statefork(
            lambda: self.runtime_manager.start(
                manager=manager,
                port=port,
                work_dir=work_dir,
                branch_id=branch_id,
            )
        )
        branch = BranchHandle(
            id=branch_id,
            port=port,
            url=f"http://{self.host}:{port}",
            base_id=base.id,
            session_id=str(environment_name),
            base_checkpoint_id=base.checkpoint_id,
            work_dir=work_dir,
            current_snapshot_id=base.checkpoint_id,
            runtime_pid=handle.pid,
            runtime_log_path=handle.log_path,
        )
        self.branches[branch_id] = branch
        self._wait_until_ready(branch)
        return branch.to_dict()

    def list_branches(self) -> list[dict[str, Any]]:
        for branch in self.branches.values():
            self._refresh_status(branch)
        return [branch.to_dict() for branch in self.branches.values()]

    def save_snapshot(
        self, branch_id: str, label: str | None = None, action: str = "manual"
    ) -> dict[str, Any]:
        branch = self._require_branch(branch_id)
        if branch.status != "running":
            raise BranchError(f"Branch {branch_id} is not running")
        # Each snapshot is a full CRIU dump on disk and the UI only clears them
        # via Reset, so cap them to keep a long-lived demo from filling the disk.
        max_snapshots = int(os.getenv("DEMO_MAX_SNAPSHOTS", "20"))
        if len(branch.snapshots) >= max_snapshots:
            raise BranchError(
                f"Snapshot limit reached ({max_snapshots}): each snapshot is a full CRIU "
                "dump on disk. Reset the workspace to clear them, or raise DEMO_MAX_SNAPSHOTS."
            )
        # The Initial snapshot already occupies index 0, so the count of existing
        # snapshots is the right number for the first user snapshot ("Snapshot 1").
        label = label or f"Snapshot {len(branch.snapshots)}"
        manager = self._require_manager(branch.base_id)

        started_at = time.time()
        with self._quiesce_branch(branch):
            snapshot_id = self._call_checkpoint_operation(manager.snapshot, failure_value=None)
            if not snapshot_id:
                raise BranchError("StateFork snapshot failed")
            # Version the external catalog data in lockstep with the CRIU dump,
            # while the runtime is still quiesced (proxy answers 503).
            self._data_tier_snapshot(snapshot_id)
        self._wait_until_ready(branch)
        self._record_operation("snapshot", started_at)

        snapshot = SnapshotHandle(
            id=str(snapshot_id),
            label=label,
            action=action,
            parent_id=branch.current_snapshot_id or branch.base_checkpoint_id,
        )
        branch.snapshots.append(snapshot)
        branch.current_snapshot_id = snapshot.id
        return {"branch": branch.to_dict(), "snapshot": snapshot.to_dict()}

    def restore_snapshot(self, branch_id: str, snapshot_id: str) -> dict[str, Any]:
        branch = self._require_branch(branch_id)
        if branch.status != "running":
            raise BranchError(f"Branch {branch_id} is not running")
        snapshot = self._require_snapshot(branch, snapshot_id)
        manager = self._require_manager(branch.base_id)

        started_at = time.time()
        with self._quiesce_branch(branch):
            ok = self._call_checkpoint_operation(
                lambda: manager.restore(snapshot.id),
                failure_value=False,
            )
            if ok is False:
                raise BranchError(f"StateFork restore failed for snapshot {snapshot.id}")
            # Roll the external catalog data back to the same snapshot's Dolt
            # branch, paired with the CRIU restore (runtime still quiesced).
            self._data_tier_restore(snapshot.id)
        self._record_operation("restore", started_at)

        branch.status = "running"
        branch.current_snapshot_id = snapshot.id
        self._wait_until_ready(branch)
        return {"branch": branch.to_dict(), "snapshot": snapshot.to_dict(), "status": "restored"}

    def merge_snapshots(
        self,
        branch_id: str,
        a_id: str,
        b_id: str,
        app_base_id: str,
        label: str | None = None,
    ) -> dict[str, Any]:
        """Combine two snapshots' Dolt catalog data into a NEW snapshot, running
        on top of a chosen app checkpoint.

        CRIU checkpoints cannot be merged, so ``app_base_id`` selects which
        checkpoint's app/cart the merged data runs on (one of the two snapshots,
        or the clean Initial snapshot). Flow: restore(app_base) [CRIU] -> merge
        the two Dolt branches onto app_base's data -> snapshot the pair. A merge
        conflict aborts the whole operation and leaves the workspace at the app
        base (app + data consistent), reporting the conflicting variants.
        """
        if self.data_tier is None:
            raise BranchError("Merge requires the external Dolt data tier.")
        branch = self._require_branch(branch_id)
        if branch.status != "running":
            raise BranchError(f"Branch {branch_id} is not running")
        a = self._require_snapshot(branch, a_id)
        b = self._require_snapshot(branch, b_id)
        base = self._require_snapshot(branch, app_base_id)
        manager = self._require_manager(branch.base_id)

        with self._quiesce_branch(branch):
            # 1. restore the chosen app checkpoint (CRIU only; Dolt handled next)
            ok = self._call_checkpoint_operation(
                lambda: manager.restore(base.id), failure_value=False
            )
            if ok is False:
                raise BranchError(f"Restore of app base {base.id} failed")
            # 2. merge the two data branches onto the app base's data
            conflicts = self.data_tier.merge_into_working(base.id, [a.id, b.id])
        branch.current_snapshot_id = base.id
        self._wait_until_ready(branch)
        if conflicts:
            raise BranchError(
                "Merge conflict on variant(s): "
                + ", ".join(conflicts)
                + ". Workspace reset to the app base; per-variant conflict "
                "resolution is not supported yet."
            )
        # 3. snapshot the merged pair (CRIU app base + committed merged data)
        return self.save_snapshot(
            branch_id,
            label=label or f"merged {a_id[:6]} + {b_id[:6]}",
            action="merge",
        )

    def reset(self) -> dict[str, Any]:
        branch_count = len(self.branches)
        base_count = len(self.bases)
        # Prune the per-snapshot Dolt branches for this workspace (best effort).
        self._data_tier_cleanup()
        for branch in list(self.branches.values()):
            branch.status = "discarded"
            self._terminate(branch)
        for manager in list(self.base_managers.values()):
            self._cleanup_manager(manager)
        self.branches.clear()
        self.bases.clear()
        self.base_managers.clear()
        self.active_base_id = None
        self.operation_stats = {"snapshot": [], "restore": []}
        return {"branches_deleted": branch_count, "bases_deleted": base_count}

    # --------------------------------------------------------------- internals

    def _record_operation(self, name: str, started_at: float) -> None:
        self.operation_stats.setdefault(name, []).append(time.time() - started_at)

    def _ensure_single_active_branch(self) -> None:
        for branch in self.branches.values():
            self._refresh_status(branch)
        active_branches = [
            branch.id for branch in self.branches.values() if branch.status == "running"
        ]
        if active_branches:
            raise BranchError(
                "StateFork backend supports one active branch at a time. "
                f"Reset the workspace first: {', '.join(active_branches)}"
            )

    def _uses_statefork_build(self) -> bool:
        # The shop apps only work in build mode (the launcher sets
        # DEMO_STATEFORK_BUILD=1); kwargs may still override it explicitly.
        return bool(self.statefork_kwargs.get("build", True))

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
            "dockerfile_dir": str(self.app.dockerfile_dir),
            "build": self._uses_statefork_build(),
            **self.statefork_kwargs,
        }
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

    def _cleanup_manager(self, manager: Any) -> None:
        try:
            self._call_statefork(manager.cleanup)
        except Exception:
            pass

    def _call_checkpoint_operation(self, fn: Callable[[], Any], *, failure_value: Any) -> Any:
        """Run a snapshot/restore against the live runtime, retrying once — a
        checkpoint taken right after the runtime resumed can fail transiently."""
        result = failure_value
        for attempt in range(2):
            result = self._call_statefork(fn)
            if result != failure_value:
                return result
            if attempt == 0:
                time.sleep(0.75)
        return result

    def _call_statefork(self, fn: Callable[[], Any]) -> Any:
        with _STATEFORK_LOCK, self._statefork_working_directory():
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
        # The StateFork controller resolves its helper binaries relative to cwd.
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
        # Website runtimes need to boot the mock API and SSR-render their first
        # page, which can take a while on a cold node.
        deadline = time.time() + float(os.getenv("DEMO_RUNTIME_READY_TIMEOUT", "60"))
        while time.time() < deadline:
            self._refresh_status(branch)
            if branch.status == "exited":
                raise BranchError(f"Branch server exited early for {branch.id}")
            try:
                with request.urlopen(f"{branch.url}{self.app.health_path}", timeout=0.5) as response:
                    if response.status == 200:
                        return
            except URLError:
                time.sleep(0.2)
        raise BranchError(f"Timed out waiting for branch server {branch.id}")

    def _refresh_status(self, branch: BranchHandle) -> None:
        if branch.status in {"discarded", "exited"}:
            return
        manager = self.base_managers.get(branch.base_id) if branch.base_id else None
        if manager is not None and branch.runtime_pid is not None:
            is_running = self._call_statefork(
                lambda: self.runtime_manager.is_running(manager=manager, pid=branch.runtime_pid)
            )
            if not is_running:
                branch.status = "exited"

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

    def _require_base(self, base_id: str) -> BaseHandle:
        base = self.bases.get(base_id)
        if base is None:
            raise BranchError(f"Unknown base checkpoint: {base_id}")
        return base

    def _require_manager(self, base_id: str | None) -> Any:
        manager = self.base_managers.get(base_id) if base_id else None
        if manager is None:
            raise BranchError(f"Base {base_id} does not have a StateFork manager")
        return manager

    @contextmanager
    def _quiesce_branch(self, branch: BranchHandle) -> Iterator[None]:
        branch.checkpointing = True
        try:
            yield
        finally:
            branch.checkpointing = False

    # Storefront runtimes are a process TREE (run-shop.sh -> mock-api + Hydrogen).
    # CRIU restore reassigns PIDs, so the tracked runtime_pid goes stale after a
    # restore and a pid-based stop then misses the tree — leaving the mock-api
    # alive on :4000 serving a stale cart across switch/reset. Since only one
    # branch runs at a time, kill the whole storefront runtime by argv marker on
    # the host (the control plane runs as root) as a reliable backstop.
    _STOREFRONT_RUNTIME_MARKERS = ("run-shop.sh", "mockapi.cjs", "server.mjs")

    def _kill_storefront_runtime_processes(self) -> None:
        if "run-shop.sh" not in self.app.runtime_command:
            return
        for proc_dir in glob.glob("/proc/[0-9]*"):
            try:
                with open(f"{proc_dir}/cmdline", "rb") as handle:
                    cmdline = handle.read().replace(b"\x00", b" ").decode("utf-8", "replace")
            except OSError:
                continue
            if any(marker in cmdline for marker in self._STOREFRONT_RUNTIME_MARKERS):
                try:
                    os.kill(int(os.path.basename(proc_dir)), signal.SIGKILL)
                except (OSError, ValueError):
                    pass

    def _terminate(self, branch: BranchHandle) -> None:
        manager = self.base_managers.get(branch.base_id) if branch.base_id else None
        if manager is not None and branch.runtime_pid is not None:
            self._call_statefork(
                lambda: self.runtime_manager.stop(manager=manager, pid=branch.runtime_pid)
            )
        self._kill_storefront_runtime_processes()
