"""Lifecycle manager for a long-lived ``dolt sql-server``.

Architecture A with realistic steady-state performance runs the external Dolt
database as a persistent MySQL-protocol server instead of spawning a ``dolt``
process per query. This helper starts/stops that server for a repo directory and
reports its connection parameters. Versioning (commit/branch/reset) then goes
through the server via ``CALL DOLT_*`` procedures (see DoltServerDataTier), never
the CLI — running CLI write commands against a live server would conflict with
the server's in-memory working set.
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("control_plane.DoltServer")

_SYSTEM_DBS = {"information_schema", "mysql", "performance_schema", "sys"}


class DoltSqlServer:
    def __init__(
        self,
        repo_dir: Path,
        host: str = "127.0.0.1",
        port: int = 3306,
        user: str = "root",
        password: str = "",
        dolt_bin: str = "dolt",
    ) -> None:
        self.repo_dir = Path(repo_dir).resolve()
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.dolt_bin = dolt_bin
        self.database: str | None = None
        self._proc: subprocess.Popen | None = None

    # ---- repo bootstrap -------------------------------------------------- #
    def ensure_repo(self) -> None:
        if shutil.which(self.dolt_bin) is None:
            raise RuntimeError(f"`{self.dolt_bin}` not found on PATH.")
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        if not (self.repo_dir / ".dolt").is_dir():
            subprocess.run(
                [self.dolt_bin, "init", "--name", "ControlPlane",
                 "--email", "control-plane@local", "--initial-branch", "main"],
                cwd=self.repo_dir, capture_output=True, text=True, check=True,
            )

    # ---- connection helpers ---------------------------------------------- #
    def _connect(self, database: str | None = None):
        import pymysql
        return pymysql.connect(
            host=self.host, port=self.port, user=self.user, password=self.password,
            database=database, autocommit=True, cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=3,
        )

    def _port_open(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            return sock.connect_ex((self.host, self.port)) == 0

    def _discover_database(self) -> str:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SHOW DATABASES")
            names = [next(iter(row.values())) for row in cur.fetchall()]
        candidates = [n for n in names if n.lower() not in _SYSTEM_DBS]
        if not candidates:
            raise RuntimeError(f"No user database on the Dolt server: {names}")
        return candidates[0]

    # ---- lifecycle ------------------------------------------------------- #
    def start(self, timeout: float = 30.0) -> None:
        """Start the server (or attach to one already on the port)."""
        self.ensure_repo()
        if self._port_open():
            logger.info(f"Reusing dolt sql-server already on {self.host}:{self.port}")
        else:
            logger.info(f"Starting dolt sql-server on {self.host}:{self.port} for {self.repo_dir}")
            self._proc = subprocess.Popen(
                [self.dolt_bin, "sql-server",
                 "--host", self.host, "--port", str(self.port)],
                cwd=self.repo_dir,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        deadline = time.time() + timeout
        last_err: Exception | None = None
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError("dolt sql-server exited during startup")
            try:
                self.database = self._discover_database()
                logger.info(f"dolt sql-server ready; database={self.database}")
                return
            except Exception as error:  # not up yet
                last_err = error
                time.sleep(0.3)
        raise RuntimeError(f"dolt sql-server did not become ready: {last_err}")

    def stop(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)
        self._proc = None

    def conn_params(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "user": self.user,
            "password": self.password,
        }
