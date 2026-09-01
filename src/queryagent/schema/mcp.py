"""Schema context adapter backed exclusively by PostgreSQL MCP."""
from __future__ import annotations

from typing import Any


class MCPSchemaRetriever:
    """Adapt MCP ``get_schema`` to the AgentLoop retriever interface."""

    def __init__(self, executor: Any, *, top_k: int | None = None) -> None:
        self.executor = executor
        self.top_k = top_k
        self.last_tables: list[str] = []
        self.last_sensitive_columns: dict[str, list[str]] = {}

    def context_for(self, question: str, *, role: str = "readonly") -> str:
        result = self.executor.get_schema(
            role=role,
            question=question,
            top_k=self.top_k,
        )
        error = result.get("error") if isinstance(result, dict) else None
        if error:
            if isinstance(error, dict):
                message = error.get("message", "MCP schema request failed")
            else:
                message = str(error)
            raise RuntimeError(message)
        self.last_tables = list(result.get("tables", []))
        self.last_sensitive_columns = {
            str(table): list(columns)
            for table, columns in (result.get("sensitive_columns") or {}).items()
        }
        return str(result.get("ddl", ""))
