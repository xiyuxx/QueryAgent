"""FastAPI boundary for the local QueryAgent Web Demo."""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from queue import Queue
from threading import Thread
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from ..agent.loop import AgentLoop
from ..llm import ProviderRegistry, RoutedLLMClient
from ..reliability.validator import ResultValidator
from ..schema.mcp import MCPSchemaRetriever
from ..tools.access import AccessConfig, load_access_config
from ..tools.mcp_client import MCPExecutor


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


@dataclass
class AppServices:
    """Lazy runtime dependencies, with injection points for unit tests."""

    registry: ProviderRegistry | None = None
    executor: Any | None = None
    schema_retriever: Any | None = None
    access_config: AccessConfig | None = None

    def get_registry(self) -> ProviderRegistry:
        if self.registry is None:
            self.registry = ProviderRegistry()
        return self.registry

    def get_access_config(self) -> AccessConfig:
        if self.access_config is None:
            self.access_config = load_access_config()
        return self.access_config

    def get_executor(self) -> Any:
        if self.executor is None:
            dsn = os.environ.get("QUERYAGENT_MCP_DSN", "").strip()
            if not dsn:
                raise RuntimeError("QUERYAGENT_MCP_DSN is not configured")
            self.executor = MCPExecutor(
                dsn,
                timeout_s=float(os.environ.get("QUERYAGENT_MCP_TIMEOUT_S", "30")),
            )
        return self.executor

    def get_schema_retriever(self) -> Any:
        if self.schema_retriever is None:
            self.schema_retriever = MCPSchemaRetriever(self.get_executor())
        return self.schema_retriever

    def build_agent(
        self,
        provider: str | None,
        event_callback,
    ) -> AgentLoop:
        llm = RoutedLLMClient(
            self.get_registry(),
            selected_provider=provider,
            event_callback=event_callback,
        )
        return AgentLoop(
            llm=llm,
            executor=self.get_executor(),
            validator=ResultValidator(),
            schema_retriever=self.get_schema_retriever(),
            enable_router=True,
            enable_summary=True,
            enable_audit=_env_bool("QUERYAGENT_ENABLE_AUDIT", False),
            max_corrections=int(os.environ.get("QUERYAGENT_MAX_CORRECTIONS", "3")),
            max_steps=int(os.environ.get("QUERYAGENT_MAX_STEPS", "5")),
        )

    def close(self) -> None:
        close = getattr(self.executor, "close", None)
        if callable(close):
            close()
        self.executor = None
        self.schema_retriever = None


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


def _role_payload(config: AccessConfig) -> list[dict[str, Any]]:
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
        allow_origins=["http://localhost:5173"],
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
        # This endpoint is intentionally cheap and does not initialize a model
        # or database connection. Readiness probes are added with the data UI.
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
                events.put(
                    (
                        "stage",
                        {
                            "stage": "model",
                            "status": "done",
                            **payload,
                        },
                    )
                )
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
                callback(
                    "error",
                    {
                        "code": "INTERNAL_ERROR",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                callback("done", {"status": "failed"})
            finally:
                events.put(None)

        thread = Thread(target=worker, daemon=True, name="queryagent-agent")
        thread.start()

        async def stream() -> AsyncIterator[str]:
            while True:
                item = await asyncio.to_thread(events.get)
                if item is None:
                    break
                event, payload = item
                # ``text`` is an internal callback name; the browser contract
                # calls these incremental pieces ``token``.
                if event == "text":
                    event = "token"
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


app = create_app()
