"""Execution protocol shared by SQLite, sandbox, PG and MCP adapters."""
from __future__ import annotations

from typing import Protocol

from .db import QueryResult


class QueryExecutor(Protocol):
    def execute(self, sql: str) -> QueryResult:
        """Execute one already-authenticated read query."""

    def close(self) -> None:
        """Release resources. Stateless executors implement this as a no-op."""
