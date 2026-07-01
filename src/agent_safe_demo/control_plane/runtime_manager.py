"""Launches an app's runtime inside checkpoint-lite's managed shell.

StateFork's build mode boots the app's container image and hands us a managed
shell (``manager.exec_command``). :class:`RuntimeManager` composes the shell
one-liner that starts the app in the background with the right environment,
then probes or stops it by PID. The whole process tree lives inside the
container, so a CRIU checkpoint captures it — including in-memory state such
as the storefront cart.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from agent_safe_demo.control_plane.manifest import interpolate_template


@dataclass(frozen=True)
class RuntimeLaunch:
    command: list[str]
    cwd: Path
    env: dict[str, str]


@dataclass(frozen=True)
class CheckpointRuntimeHandle:
    pid: int
    log_path: str


class RuntimeManager:
    def __init__(
        self,
        *,
        host: str,
        command: str,
        cwd: str = "/",
        port_env: str = "PORT",
        env: dict[str, str] | None = None,
    ) -> None:
        self.host = host
        self.command = command
        self.cwd = cwd
        self.port_env = port_env
        self.env = dict(env or {})

    def build_launch(self, *, port: int, work_dir: Path) -> RuntimeLaunch:
        variables = {
            "PORT": str(port),
            "HOST": self.host,
            "BRANCH_WORKDIR": str(work_dir),
        }
        env = {self.port_env: str(port)}
        for key, value in self.env.items():
            env[key] = interpolate_template(value, variables)
        command = shlex.split(interpolate_template(self.command, variables))
        if not command:
            raise ValueError("Runtime command is empty")
        # cwd is a path INSIDE the container (the manifest declares it).
        cwd = Path(interpolate_template(self.cwd or "/", variables))
        return RuntimeLaunch(command=command, cwd=cwd, env=env)

    def start(
        self,
        *,
        manager,
        port: int,
        work_dir: Path,
        branch_id: str,
    ) -> CheckpointRuntimeHandle:
        launch = self.build_launch(port=port, work_dir=work_dir)
        log_path = f"/tmp/{branch_id}-runtime.log"
        assignments = " ".join(
            f"{shlex.quote(key)}={shlex.quote(value)}"
            for key, value in sorted(launch.env.items())
        )
        command = shlex.join(launch.command)
        cwd = shlex.quote(str(launch.cwd))
        script = (
            f"cd {cwd} && "
            f"{assignments} {command} > {shlex.quote(log_path)} 2>&1 & "
            "printf 'RUNTIME_PID=%s\\n' \"$!\""
        )
        returncode, stdout, stderr = manager.exec_command(script, timeout=10)
        if returncode != 0:
            raise RuntimeError(f"checkpoint exec launch failed: {stderr or stdout}")
        match = re.search(r"RUNTIME_PID=(\d+)", stdout)
        if match is None:
            raise RuntimeError(f"checkpoint exec launch did not return a runtime PID: {stdout!r}")
        return CheckpointRuntimeHandle(pid=int(match.group(1)), log_path=log_path)

    def is_running(self, *, manager, pid: int) -> bool:
        script = f"if kill -0 {int(pid)} 2>/dev/null; then echo RUNTIME_ALIVE=1; else echo RUNTIME_ALIVE=0; fi"
        _, stdout, _ = manager.exec_command(script, timeout=5)
        return "RUNTIME_ALIVE=1" in stdout

    def stop(self, *, manager, pid: int) -> None:
        pid = int(pid)
        script = (
            f"kill -TERM {pid} 2>/dev/null || true; "
            f"for i in 1 2 3 4 5; do "
            f"if ! kill -0 {pid} 2>/dev/null; then break; fi; "
            "sleep 1; "
            "done; "
            f"if kill -0 {pid} 2>/dev/null; then kill -KILL {pid} 2>/dev/null || true; fi; "
            "echo RUNTIME_STOPPED=1"
        )
        manager.exec_command(script, timeout=10)
