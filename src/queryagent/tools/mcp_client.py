"""Synchronous adapter for the QueryAgent MCP data tools.

A dedicated event-loop thread owns the stdio session. The public methods are
safe for the synchronous AgentLoop and expose a close() lifecycle hook so eval
runs do not leak MCP server processes.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from .db import QueryResult
from .policy import SQLPolicyResult

_SRC_ROOT = str(Path(__file__).resolve().parents[2])
_SQLITE_SERVER_MODULE = "queryagent.tools.mcp_server"
_POSTGRES_SERVER_MODULE = "queryagent.tools.postgres_mcp_server"


class MCPExecutor:
    def __init__(
        self,
        database: str,
        *,
        backend: str = "subprocess",
        timeout_s: float = 30.0,
        role: str = "",
        server_module: str | None = None,
    ) -> None:
        """Open an MCP session for a database connection.

        ``database`` is a SQLite path only for the historical compatibility
        server. PostgreSQL is selected explicitly with a ``postgresql://``
        DSN or ``server_module`` and receives the DSN through the child
        process environment rather than its command line.
        """
        self.database = database
        self.db_path = database  # old callers inspect this attribute
        self.backend = backend
        self.timeout_s = timeout_s
        self.default_role = role
        self.server_module = server_module or (
            _POSTGRES_SERVER_MODULE
            if database.startswith(("postgres://", "postgresql://"))
            else _SQLITE_SERVER_MODULE
        )
        if self.server_module == _POSTGRES_SERVER_MODULE and not database.startswith(("postgres://", "postgresql://")):
            raise ValueError("PostgreSQL MCP requires a postgres:// or postgresql:// DSN")
        self._loop = asyncio.new_event_loop()
        self._session = None
        self._stdio_cm = None
        self._error: str | None = None
        self._closed = False
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="queryagent-mcp")
        self._thread.start()
        if not self._ready.wait(timeout=30):
            self.close()
            raise RuntimeError("MCP server did not become ready in time")
        if self._error:
            self.close()
            raise RuntimeError(f"MCP init failed: {self._error}")

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.create_task(self._connect_and_signal())
        self._loop.run_forever()

    async def _connect_and_signal(self) -> None:
        try:
            await self._connect()
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"
        finally:
            self._ready.set()

    async def _connect(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        inherited_keys = (
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "QUERYAGENT_ROLES_CONFIG",
            "QUERYAGENT_AUDIT_LOG",
            "QUERYAGENT_MAX_ROWS",
            "QUERYAGENT_STATEMENT_TIMEOUT_MS",
        )
        env = {
            key: os.environ[key]
            for key in inherited_keys
            if key in os.environ
        }
        env["PYTHONPATH"] = _SRC_ROOT + os.pathsep + os.environ.get("PYTHONPATH", "")
        if self.server_module == _POSTGRES_SERVER_MODULE:
            # Do not forward QUERYAGENT_DB_DSN/QUERYAGENT_ADMIN_DSN or model
            # credentials to the child; MCP receives only the reader DSN.
            env["QUERYAGENT_MCP_DSN"] = self.database
        args = ["-m", self.server_module]
        if self.server_module == _SQLITE_SERVER_MODULE:
            args.extend([self.database, self.backend])
        params = StdioServerParameters(
            command=sys.executable,
            args=args,
            env=env,
        )
        self._stdio_cm = stdio_client(params)
        read, write = await self._stdio_cm.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()

    def _call_sync(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._closed:
            return {"error": {"code": "MCP_CLOSED", "message": "MCP executor is closed"}}
        if self._session is None:
            return {"error": {"code": "MCP_UNAVAILABLE", "message": self._error or "MCP session not initialized"}}
        future = asyncio.run_coroutine_threadsafe(self._call(tool_name, arguments), self._loop)
        try:
            return future.result(timeout=self.timeout_s)
        except Exception as exc:  # noqa: BLE001
            return {"error": {"code": "MCP_CALL_FAILED", "message": f"{type(exc).__name__}: {exc}"}}

    async def _call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._session.call_tool(tool_name, arguments)
        structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
        if structured:
            return dict(structured)
        if not result.content:
            return {"error": {"code": "EMPTY_TOOL_RESULT", "message": "empty tool result"}}
        first = result.content[0]
        text = getattr(first, "text", None) or str(first)
        return json.loads(text)

    def validate_sql(self, sql: str, role: str | None = None) -> SQLPolicyResult:
        effective_role = self.default_role if role is None else role
        data = self._call_sync("validate_sql", {"sql": sql, "role": effective_role})
        if "error" in data:
            error = data["error"]
            return SQLPolicyResult(False, error.get("code", "MCP_ERROR"), error.get("message", "MCP error"))
        return SQLPolicyResult(
            bool(data.get("ok")),
            data.get("code", "INVALID_SQL"),
            data.get("message", ""),
            data.get("normalized_sql", ""),
            list(data.get("tables", [])),
        )

    def get_schema(self, role: str | None = None, **kwargs: Any) -> dict[str, Any]:
        effective_role = self.default_role if role is None else role
        return self._call_sync("get_schema", {"role": effective_role, **kwargs})

    def search_values(self, term: str, role: str | None = None, limit: int = 5) -> dict[str, Any]:
        effective_role = self.default_role if role is None else role
        return self._call_sync("search_values", {"term": term, "role": effective_role, "limit": limit})

    def list_tables(self, role: str | None = None) -> dict[str, Any]:
        effective_role = self.default_role if role is None else role
        return self._call_sync("list_tables", {"role": effective_role})

    def browse_table(
        self,
        table: str,
        role: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        effective_role = self.default_role if role is None else role
        return self._call_sync(
            "browse_table",
            {"table": table, "role": effective_role, "page": page, "page_size": page_size},
        )

    def search_table(
        self,
        table: str,
        term: str,
        role: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        effective_role = self.default_role if role is None else role
        return self._call_sync(
            "search_table",
            {
                "table": table,
                "term": term,
                "role": effective_role,
                "page": page,
                "page_size": page_size,
            },
        )

    def export_table_csv(
        self,
        table: str,
        role: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        effective_role = self.default_role if role is None else role
        return self._call_sync(
            "export_table_csv",
            {
                "table": table,
                "role": effective_role,
                "page": page,
                "page_size": page_size,
            },
        )

    def export_query_csv(self, sql: str, role: str | None = None) -> dict[str, Any]:
        effective_role = self.default_role if role is None else role
        return self._call_sync(
            "export_query_csv",
            {"sql": sql, "role": effective_role},
        )

    def execute(self, sql: str, role: str | None = None) -> QueryResult:
        effective_role = self.default_role if role is None else role
        data = self._call_sync("query", {"sql": sql, "role": effective_role})
        error = data.get("error")
        if isinstance(error, dict):
            error = f"{error.get('code', 'MCP_ERROR')}: {error.get('message', '')}"
        return QueryResult(
            columns=list(data.get("columns", [])),
            rows=[tuple(row) for row in data.get("rows", [])],
            truncated=bool(data.get("truncated", False)),
            error=error,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
            try:
                future.result(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        if not self._loop.is_closed():
            self._loop.close()

    async def _shutdown(self) -> None:
        if self._session is not None:
            await self._session.__aexit__(None, None, None)
            self._session = None
        if self._stdio_cm is not None:
            await self._stdio_cm.__aexit__(None, None, None)
            self._stdio_cm = None

    def __enter__(self) -> "MCPExecutor":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
