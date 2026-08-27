"""SQLite 执行器：只读（仅 SELECT）、超时、行数上限、禁止多语句。

真正的隔离执行（沙箱/Docker）见 tools/sandbox.py；这里提供进程内执行原语与共用的
SQL 安全守卫 check_sql。MCP 工具封装见 tools/mcp_server.py。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .policy import check_sql

_MAX_ROWS = 100
_TIMEOUT_S = 5.0


class QueryError(Exception):
    """SQL 执行失败（错误信息可回灌给 LLM 自纠正）。"""


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    error: str | None = None
    truncated: bool = False


class SQLiteExecutor:
    def __init__(
        self, db_path: str, *, max_rows: int = _MAX_ROWS, timeout_s: float = _TIMEOUT_S
    ) -> None:
        self.db_path = db_path
        self.max_rows = max_rows
        self.timeout_s = timeout_s

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=self.timeout_s)

    def close(self) -> None:
        """SQLiteExecutor is stateless; kept for the shared executor protocol."""

    def execute(self, sql: str) -> QueryResult:
        guard = check_sql(sql)
        if guard is not None:
            return QueryResult(error=guard)

        try:
            conn = self._connect()
            try:
                cur = conn.execute(sql)
                columns = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchmany(self.max_rows + 1)
                truncated = len(rows) > self.max_rows
                if truncated:
                    rows = rows[: self.max_rows]
                return QueryResult(columns=columns, rows=rows, truncated=truncated)
            finally:
                conn.close()
        except sqlite3.Error as e:
            return QueryResult(error=f"{type(e).__name__}: {e}")
