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
_SERVER_MODULE = "queryagent.tools.mcp_server"


class MCPExecutor:
    def __init__(
        self,
        db_path: str,
        *,
        backend: str = "subprocess",
        timeout_s: float = 30.0,
    ) -> None:
        self.db_path = db_path
        self.backend = backend
        self.timeout_s = timeout_s
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

        env = {
            **os.environ,
            "PYTHONPATH": _SRC_ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""),
        }
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", _SERVER_MODULE, self.db_path, self.backend],
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

    def validate_sql(self, sql: str, role: str = "") -> SQLPolicyResult:
        data = self._call_sync("validate_sql", {"sql": sql, "role": role})
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

    def get_schema(self, role: str = "") -> dict[str, Any]:
        return self._call_sync("get_schema", {"role": role})

    def execute(self, sql: str, role: str = "") -> QueryResult:
        data = self._call_sync("query", {"sql": sql, "role": role})
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
