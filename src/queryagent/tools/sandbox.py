"""SQL 沙箱执行器：在隔离进程/容器内执行 SQL。

- subprocess 后端：子进程 + 只读守卫（本机可用，零外部依赖，可实测）。
- docker 后端：Docker 容器 + 只读挂载（需 Docker daemon，DESIGN 目标）。

两种后端都实现 SQLiteExecutor 同款 execute(sql) -> QueryResult 接口，
agent loop 无感切换。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .db import QueryResult, check_sql

_WORKER = Path(__file__).parent / "_sandbox_worker.py"
_DOCKER_IMAGE = "python:3.11-slim"


class SandboxExecutor:
    def __init__(
        self,
        db_path: str,
        *,
        backend: str = "subprocess",
        timeout_s: float = 5.0,
        max_rows: int = 100,
    ) -> None:
        self.db_path = db_path
        self.backend = backend
        self.timeout_s = timeout_s
        self.max_rows = max_rows

    def close(self) -> None:
        """Sandbox processes are created per query and require no cleanup."""

    def execute(self, sql: str) -> QueryResult:
        guard = check_sql(sql)
        if guard is not None:
            return QueryResult(error=guard)

        try:
            if self.backend == "subprocess":
                out = self._run_subprocess(sql)
            elif self.backend == "docker":
                out = self._run_docker(sql)
            else:
                return QueryResult(error=f"unknown sandbox backend: {self.backend}")
        except subprocess.TimeoutExpired as e:
            stderr = (e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            return QueryResult(error=f"query timed out after {self.timeout_s}s: {stderr[:200]}")
        except Exception as e:  # noqa: BLE001
            return QueryResult(error=f"sandbox error: {type(e).__name__}: {e}")

        return QueryResult(
            columns=out.get("columns", []),
            rows=[tuple(r) for r in out.get("rows", [])],
            truncated=out.get("truncated", False),
            error=out.get("error"),
        )

    def _run_subprocess(self, sql: str) -> dict:
        db_abs = str(Path(self.db_path).resolve())
        proc = subprocess.run(
            [sys.executable, str(_WORKER), db_abs, sql, str(self.max_rows)],
            capture_output=True,
            text=True,
            timeout=self.timeout_s,
            stdin=subprocess.DEVNULL,
        )
        return json.loads(proc.stdout)

    def _run_docker(self, sql: str) -> dict:
        db_abs = str(Path(self.db_path).resolve())
        cmd = [
            "docker", "run", "--rm",
            "--network", "none",
            "--memory", "256m",
            "-v", f"{db_abs}:/db/db.sqlite:ro",
            "-v", f"{_WORKER}:/worker.py:ro",
            _DOCKER_IMAGE,
            "python", "/worker.py", "/db/db.sqlite", sql, str(self.max_rows),
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=self.timeout_s + 60
        )
        # 容器可能打印镜像拉取进度等噪音，取最后一行 JSON
        last_line = proc.stdout.strip().splitlines()[-1]
        return json.loads(last_line)
