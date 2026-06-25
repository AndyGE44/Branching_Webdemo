from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from agent_safe_demo.control_plane.manifest import interpolate_template


@dataclass(frozen=True)
class RuntimeLaunch:
    command: list[str]
    cwd: Path
    env: dict[str, str]
    variables: dict[str, str]


@dataclass(frozen=True)
class CheckpointRuntimeHandle:
    pid: int
    log_path: str


def pythonpath_for(root: Path) -> str:
    src_path = str(root / "src")
    existing = os.environ.get("PYTHONPATH")
    if existing:
        return f"{src_path}{os.pathsep}{existing}"
    return src_path


def runtime_pythonpath_for(project_root: Path, work_dir: Path) -> str:
    root = work_dir if (work_dir / "src").exists() else project_root
    return pythonpath_for(root)


class RuntimeProcessManager:
    """Starts manifest-described app runtimes inside a branch workspace."""

    def __init__(
        self,
        *,
        project_root: Path,
        host: str,
        app_uvicorn_target: str,
        app_db_env_var: str,
        runtime_command: str | None = None,
        runtime_cwd: str = ".",
        runtime_port_env: str = "PORT",
        state_env: dict[str, str] | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.host = host
        self.app_uvicorn_target = app_uvicorn_target
        self.app_db_env_var = app_db_env_var
        self.runtime_command = runtime_command
        self.runtime_cwd = runtime_cwd
        self.runtime_port_env = runtime_port_env
        self.state_env = dict(state_env or {})

    def build_launch(self, *, db_path: Path, port: int, work_dir: Path) -> RuntimeLaunch:
        variables = self.runtime_variables(db_path=db_path, port=port, work_dir=work_dir)
        env = os.environ.copy()
        env[self.app_db_env_var] = str(db_path)
        env[self.runtime_port_env] = str(port)
        for key, value in self.state_env.items():
            env[key] = interpolate_template(value, variables)
        env["PYTHONPATH"] = runtime_pythonpath_for(self.project_root, work_dir)

        return RuntimeLaunch(
            command=self.runtime_command_args(variables),
            cwd=self.runtime_working_directory(work_dir, variables),
            env=env,
            variables=variables,
        )

    def start(self, *, db_path: Path, port: int, work_dir: Path) -> subprocess.Popen:
        launch = self.build_launch(db_path=db_path, port=port, work_dir=work_dir)
        return subprocess.Popen(
            launch.command,
            cwd=launch.cwd,
            env=launch.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def runtime_variables(self, *, db_path: Path, port: int, work_dir: Path) -> dict[str, str]:
        return {
            "PORT": str(port),
            "HOST": self.host,
            "BRANCH_WORKDIR": str(work_dir),
            "PROJECT_ROOT": str(self.project_root),
            "PRIMARY_STATE_PATH": str(db_path),
        }

    def runtime_working_directory(self, work_dir: Path, variables: dict[str, str]) -> Path:
        raw_cwd = interpolate_template(self.runtime_cwd or ".", variables)
        path = Path(raw_cwd)
        if path.is_absolute():
            return path
        return work_dir / path

    def runtime_command_args(self, variables: dict[str, str]) -> list[str]:
        if self.runtime_command:
            command = shlex.split(interpolate_template(self.runtime_command, variables))
        else:
            command = [
                sys.executable,
                "-m",
                "uvicorn",
                self.app_uvicorn_target,
                "--host",
                self.host,
                "--port",
                str(variables["PORT"]),
            ]
        if not command:
            raise ValueError("Runtime command is empty")
        return command


class CheckpointExecRuntimeManager(RuntimeProcessManager):
    """Starts app runtimes as children of checkpoint-lite's managed shell."""

    def build_launch(self, *, db_path: Path, port: int, work_dir: Path) -> RuntimeLaunch:
        variables = self.runtime_variables(db_path=db_path, port=port, work_dir=work_dir)
        env = {}
        if self.app_db_env_var:
            env[self.app_db_env_var] = f"/{db_path.name}"
        env[self.runtime_port_env] = str(port)
        for key, value in self.state_env.items():
            env[key] = interpolate_template(value, variables)
        env["PYTHONPATH"] = "/src"
        return RuntimeLaunch(
            command=self.runtime_command_args(variables),
            cwd=self.runtime_working_directory(work_dir, variables),
            env=env,
            variables=variables,
        )

    def runtime_working_directory(self, work_dir: Path, variables: dict[str, str]) -> Path:
        raw_cwd = interpolate_template(self.runtime_cwd or "/", variables)
        return Path(raw_cwd)

    def start(
        self,
        *,
        manager,
        db_path: Path,
        port: int,
        work_dir: Path,
        branch_id: str,
    ) -> CheckpointRuntimeHandle:
        launch = self.build_launch(db_path=db_path, port=port, work_dir=work_dir)
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
