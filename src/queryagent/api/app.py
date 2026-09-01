"""FastAPI boundary for the local QueryAgent Web Demo."""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from queue import Queue
from threading import Thread
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .runtime import AppServices
from ..agent.loop import AgentLoop


class ChatHistoryItem(BaseModel):
    """A persisted local turn; unknown frontend fields are ignored."""

    role: str = "readonly"
    access_role: str | None = None
    question: str = ""
    answer: str = ""
    sql: str = ""
    result_summary: str = ""
    content: str = ""

    model_config = {"extra": "ignore"}


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    role: str = Field(default="readonly", min_length=1, max_length=64)
    provider: str | None = Field(default=None, max_length=64)
    history: list[ChatHistoryItem] = Field(default_factory=list, max_length=50)

    @field_validator("question")
    @classmethod
    def question_must_contain_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("question must not be blank")
        return value

    @field_validator("role")
    @classmethod
    def normalize_role(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str | None) -> str | None:
        return value.strip().lower() if value else None


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _sse(event: str, payload: dict[str, Any]) -> str:
    return (
        f"event: {event}\n"
        f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


def _history_payload(items: list[ChatHistoryItem]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items:
        data = item.model_dump(exclude_none=True)
        if item.access_role:
            data["role"] = item.access_role
        result.append(data)
    return result


def _role_payload(config) -> list[dict[str, Any]]:
    roles: list[dict[str, Any]] = []
    for name, policy in sorted(config.roles.items()):
        roles.append(
            {
                "name": name,
                "allowed_tables": (
                    None
                    if policy.allowed_tables is None
                    else sorted(policy.allowed_tables)
                ),
            }
        )
    return roles


def create_app(*, services: AppServices | None = None) -> FastAPI:
    runtime = services or AppServices()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        runtime.close()

    application = FastAPI(
        title="QueryAgent API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.services = runtime
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "https://cohub.live",
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @application.get("/api/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": "queryagent-api",
            "version": application.version,
            "time": datetime.now(UTC).isoformat(),
        }

    @application.get("/api/system/status")
    def system_status() -> dict[str, object]:
        providers = {
            name: bool(os.environ.get(f"{name.upper()}_API_KEY", "").strip())
            for name in ("deepseek", "qwen", "openai")
        }
        return {
            "status": "foundation",
            "providers": providers,
            "database": {"status": "pending", "backend": "postgresql"},
            "embedding": {"status": "pending"},
        }

    @application.get("/api/config/providers")
    def provider_config() -> dict[str, Any]:
        registry = runtime.get_registry()
        return {
            "default_provider": registry.default_provider,
            "providers": registry.public_status(),
        }

    @application.get("/api/roles")
    def roles() -> dict[str, Any]:
        config = runtime.get_access_config()
        return {
            "default_role": config.default_role,
            "roles": _role_payload(config),
        }

    @application.get("/api/data/tables")
    def data_tables(role: str = "readonly") -> dict[str, Any]:
        config = runtime.get_access_config()
        role = role.strip().lower()
        if config.get_role(role) is None:
            raise HTTPException(status_code=400, detail=f"unknown role: {role}")
        result = runtime.get_executor().list_tables(role=role)
        return _mcp_or_http_error(result)

    @application.get("/api/data/table/{table}")
    def data_table(
        table: str,
        role: str = "readonly",
        page: int = 1,
        page_size: int = 50,
        search: str = "",
    ) -> dict[str, Any]:
        config = runtime.get_access_config()
        role = role.strip().lower()
        if config.get_role(role) is None:
            raise HTTPException(status_code=400, detail=f"unknown role: {role}")
        if page < 1 or page_size < 1 or page_size > 100:
            raise HTTPException(status_code=400, detail="page must be >= 1 and page_size must be 1..100")
        executor = runtime.get_executor()
        result = (
            executor.search_table(table, search, role=role, page=page, page_size=page_size)
            if search.strip()
            else executor.browse_table(table, role=role, page=page, page_size=page_size)
        )
        return _mcp_or_http_error(result)

    @application.get("/api/data/table/{table}/csv")
    def data_table_csv(
        table: str,
        role: str = "readonly",
        page: int = 1,
        page_size: int = 50,
    ) -> StreamingResponse:
        config = runtime.get_access_config()
        role = role.strip().lower()
        if config.get_role(role) is None:
            raise HTTPException(status_code=400, detail=f"unknown role: {role}")
        if page < 1 or page_size < 1 or page_size > 100:
            raise HTTPException(status_code=400, detail="page must be >= 1 and page_size must be 1..100")
        result = runtime.get_executor().export_table_csv(
            table, role=role, page=page, page_size=page_size
        )
        if result.get("error"):
            raise _mcp_http_exception(result)
        csv_text = result.get("csv", "")
        filename = result.get("filename", f"{table}.csv")
        return StreamingResponse(
            iter([csv_text]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @application.post("/api/evaluations")
    def create_evaluation(payload: dict[str, Any]) -> dict[str, Any]:
        dataset = str(payload.get("dataset", "mini")).strip().lower()
        if dataset not in {"mini", "warehouse"}:
            raise HTTPException(status_code=400, detail="dataset must be mini or warehouse")
        registry = runtime.get_registry()
        selected = str(payload.get("provider") or registry.default_provider).lower()
        if selected not in {item.name for item in registry.available()}:
            raise HTTPException(status_code=400, detail=f"provider {selected!r} 未配置")
        run = runtime.evaluation_manager.create(dataset)
        runtime.evaluation_manager.start(
            run,
            runtime.build_evaluation_worker(dataset, selected),
        )
        return run.public_dict()

    @application.get("/api/evaluations/{run_id}")
    def get_evaluation(run_id: str) -> dict[str, Any]:
        run = runtime.evaluation_manager.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="evaluation run not found")
        return run.public_dict()

    @application.post("/api/chat/stream")
    async def chat_stream(request: ChatRequest) -> StreamingResponse:
        registry = runtime.get_registry()
        role = request.role
        if runtime.get_access_config().get_role(role) is None:
            raise HTTPException(status_code=400, detail=f"unknown role: {role}")

        available = {config.name for config in registry.available()}
        selected = request.provider or registry.default_provider
        if not available:
            raise HTTPException(status_code=503, detail="没有已配置的模型 Provider")
        if selected not in available:
            raise HTTPException(status_code=400, detail=f"provider {selected!r} 未配置")

        history = _history_payload(request.history)
        events: Queue[tuple[str, dict[str, Any]] | None] = Queue()

        def callback(event: str, payload: dict[str, Any]) -> None:
            if event == "provider":
                events.put(("stage", {"stage": "model", "status": "done", **payload}))
            else:
                events.put((event, payload))

        def worker() -> None:
            try:
                agent = runtime.build_agent(selected, callback)
                agent.run(
                    request.question,
                    role=role,
                    history=history,
                    event_callback=callback,
                )
            except Exception as exc:  # never expose a worker traceback in SSE
                callback("error", {"code": "INTERNAL_ERROR", "error": f"{type(exc).__name__}: {exc}"})
                callback("done", {"status": "failed"})
            finally:
                events.put(None)

        Thread(target=worker, daemon=True, name="queryagent-agent").start()

        async def stream() -> AsyncIterator[str]:
            while True:
                item = await asyncio.to_thread(events.get)
                if item is None:
                    break
                event, payload = item
                yield _sse(event, payload)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return application


def _mcp_or_http_error(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("error"):
        raise _mcp_http_exception(result)
    return result


def _mcp_http_exception(result: dict[str, Any]) -> HTTPException:
    error = result.get("error") or {}
    code = error.get("code", "MCP_ERROR") if isinstance(error, dict) else "MCP_ERROR"
    message = error.get("message", "MCP request failed") if isinstance(error, dict) else str(error)
    status = 403 if code in {"ACCESS_DENIED", "TABLE_NOT_ALLOWED", "SENSITIVE_COLUMN"} else 400
    return HTTPException(status_code=status, detail={"code": code, "message": message})


app = create_app()
