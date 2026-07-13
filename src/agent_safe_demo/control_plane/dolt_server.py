"""Lifecycle manager for a long-lived ``dolt sql-server`` (architecture A).

The storefront runs *inside* Waypoint (chroot, host network namespace) while its
pricing/inventory data lives *outside* in an external Dolt database on the host.
This helper starts one ``dolt sql-server`` for the demo, hosts one database per
shop, and reports connection parameters. Versioning (commit/branch/reset) then
goes through the server via ``CALL DOLT_*`` procedures (see
:class:`~agent_safe_demo.control_plane.data_tier.DoltServerDataTier`), never the
CLI — CLI writes against a live server would fight its in-memory working set.

Because the shop's mock Storefront API reaches ``127.0.0.1:<port>`` on the host
loopback (no network namespace), the server is directly dialable from inside the
checkpointed runtime.
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
    """A single ``dolt sql-server`` process hosting one database per shop.

    Started in ``base_dir`` (a plain directory, *not* itself a Dolt repo); each
    shop database is created on demand with ``CREATE DATABASE`` so it becomes an
    independently branchable Dolt database under ``base_dir/<name>``.
    """

    def __init__(
        self,
        base_dir: Path,
        host: str = "127.0.0.1",
        port: int = 3306,
        user: str = "root",
        password: str = "",
        dolt_bin: str = "dolt",
    ) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.dolt_bin = dolt_bin
        self._proc: subprocess.Popen | None = None

    # ---- connection helpers ---------------------------------------------- #
    def _connect(self, database: str | None = None):
        import pymysql

        return pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=database,
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=3,
        )

    def _port_open(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            return sock.connect_ex((self.host, self.port)) == 0

    # ---- lifecycle ------------------------------------------------------- #
    def start(self, timeout: float = 30.0) -> None:
        """Start the server (or attach to one already on the port)."""
        if shutil.which(self.dolt_bin) is None:
            raise RuntimeError(f"`{self.dolt_bin}` not found on PATH.")
        self.base_dir.mkdir(parents=True, exist_ok=True)
        if self._port_open():
            logger.info("Reusing dolt sql-server already on %s:%s", self.host, self.port)
        else:
            logger.info(
                "Starting dolt sql-server on %s:%s for %s",
                self.host,
                self.port,
                self.base_dir,
            )
            self._proc = subprocess.Popen(
                [self.dolt_bin, "sql-server", "--host", self.host, "--port", str(self.port)],
                cwd=self.base_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        deadline = time.time() + timeout
        last_err: Exception | None = None
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError("dolt sql-server exited during startup")
            try:
                with self._connect() as conn, conn.cursor() as cur:
                    cur.execute("SELECT 1")
                logger.info("dolt sql-server ready on %s:%s", self.host, self.port)
                return
            except Exception as error:  # not up yet
                last_err = error
                time.sleep(0.3)
        raise RuntimeError(f"dolt sql-server did not become ready: {last_err}")

    def ensure_database(self, name: str) -> None:
        """Create the shop database if it does not exist (idempotent)."""
        with self._connect() as conn, conn.cursor() as cur:
            # Dolt database names cannot contain a hyphen; shop ids use underscores
            # already, but guard anyway.
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{name}`")

    def conn_params(self, database: str) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "database": database,
            "user": self.user,
            "password": self.password,
        }

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
