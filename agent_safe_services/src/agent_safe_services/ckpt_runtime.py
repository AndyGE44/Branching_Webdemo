from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class CheckpointLiteError(RuntimeError):
    pass


@dataclass(frozen=True)
class InitResult:
    session_id: str
    workdir: Path


class CheckpointLiteRuntime:
    def __init__(self, binary: str, sessions_dir: str, session_info_dir: str | None = None, use_sudo: bool = True) -> None:
        self.binary = binary
        self.sessions_dir = sessions_dir
        self.session_info_dir = session_info_dir
        self.use_sudo = use_sudo

    def _env_args(self) -> list[str]:
        args = [f"CHECKPOINT_SESSIONS_DIR={self.sessions_dir}"]
        if self.session_info_dir:
            args.append(f"CHECKPOINT_SESSION_INFO_DIR={self.session_info_dir}")
        return args

    def _cmd(self, *args: str) -> list[str]:
        base = [self.binary, *args]
        if self.use_sudo and os.geteuid() != 0:
            return ["sudo", "-n", "env", *self._env_args(), *base]
        return ["env", *self._env_args(), *base]

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(self._cmd(*args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or str(proc.returncode)
            raise CheckpointLiteError(f"checkpoint-lite {' '.join(args)} failed: {detail}")
        return proc

    def init(self, source_dir: Path) -> InitResult:
        proc = self._run("init", str(source_dir), "--quiet")
        line = proc.stdout.strip().splitlines()[-1]
        try:
            session_id, workdir = line.split(",", 1)
        except ValueError as exc:
            raise CheckpointLiteError(f"unexpected init output: {line}") from exc
        return InitResult(session_id=session_id, workdir=Path(workdir))

    def create(self, session_id: str, checkpoint_id: str, pid: int = -1) -> None:
        self._run("create", session_id, checkpoint_id, str(pid))

    def restore(self, session_id: str, checkpoint_id: str) -> None:
        self._run("restore", session_id, checkpoint_id)

    def cleanup(self, session_id: str) -> None:
        subprocess.run(self._cmd("cleanup", session_id, "--force"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
