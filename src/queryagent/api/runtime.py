"""Web runtime services shared by API routes."""
from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from ..agent.loop import AgentLoop
from ..eval.dataset import EvalCase, load_dataset
from ..eval.harness import EvalHarness
from ..eval.metrics import RunReport
from ..llm import ProviderRegistry, RoutedLLMClient
from ..reliability.validator import ResultValidator
from ..schema.mcp import MCPSchemaRetriever
from ..tools.access import AccessConfig, load_access_config
from ..tools.mcp_client import MCPExecutor


@dataclass
class EvaluationRun:
    run_id: str
    dataset: str
    status: str = "queued"
    started_at: str | None = None
    finished_at: str | None = None
    report: RunReport | None = None
    error: str | None = None

    def public_dict(self) -> dict[str, Any]:
        report = self.report
        cases = []
        if report is not None:
            cases = [
                {
                    "case_id": case.case_id,
                    "question": case.question,
                    "sql": case.sql,
                    "gold_sql": case.gold_sql,
                    "status": case.status,
                    "exec_match": case.exec_match,
                    "exact_match": case.exact_match,
                    "steps": case.steps,
                    "corrections": case.corrections,
                    "tokens": case.tokens,
                    "cost_usd": case.cost_usd,
                    "latency_ms": case.latency_ms,
                    "error": case.error,
                }
                for case in report.cases
            ]
        return {
            "run_id": self.run_id,
            "dataset": self.dataset,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "summary": (
                {
                    "total": report.total,
                    "executed": report.executed,
                    "exec_accuracy": report.exec_accuracy,
                    "exact_match_rate": report.exact_match_rate,
                    "avg_steps": report.avg_steps,
                    "avg_corrections": report.avg_corrections,
                    "avg_tokens": report.avg_tokens,
                    "total_cost_usd": report.total_cost_usd,
                    "avg_latency_ms": report.avg_latency_ms,
                }
                if report is not None
                else None
            ),
            "cases": cases,
        }


@dataclass
class EvaluationManager:
    """In-memory evaluation task manager; tasks disappear on process restart."""

    runs: dict[str, EvaluationRun] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def get(self, run_id: str) -> EvaluationRun | None:
        with self._lock:
            return self.runs.get(run_id)

    def create(self, dataset: str) -> EvaluationRun:
        run = EvaluationRun(run_id=uuid.uuid4().hex, dataset=dataset)
        with self._lock:
            self.runs[run.run_id] = run
        return run

    def start(
        self,
        run: EvaluationRun,
        worker: Callable[[], RunReport],
    ) -> None:
        def execute() -> None:
            run.status = "running"
            run.started_at = datetime.now(UTC).isoformat()
            try:
                run.report = worker()
                run.status = "completed"
            except Exception as exc:  # noqa: BLE001
                run.error = f"{type(exc).__name__}: {exc}"
                run.status = "failed"
            finally:
                run.finished_at = datetime.now(UTC).isoformat()

        threading.Thread(
            target=execute,
            daemon=True,
            name=f"queryagent-eval-{run.run_id[:8]}",
        ).start()


@dataclass
class AppServices:
    """Lazy runtime dependencies, with injection points for unit tests."""

    registry: ProviderRegistry | None = None
    executor: Any | None = None
    schema_retriever: Any | None = None
    access_config: AccessConfig | None = None
    evaluation_manager: EvaluationManager = field(default_factory=EvaluationManager)

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
        event_callback=None,
        *,
        enable_summary: bool = True,
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
            enable_summary=enable_summary,
            enable_audit=_env_bool("QUERYAGENT_ENABLE_AUDIT", False),
            max_corrections=int(os.environ.get("QUERYAGENT_MAX_CORRECTIONS", "3")),
            max_steps=int(os.environ.get("QUERYAGENT_MAX_STEPS", "5")),
        )

    def dataset_path(self, dataset: str) -> Path:
        if dataset not in {"mini", "warehouse"}:
            raise ValueError("dataset must be mini or warehouse")
        root = Path(__file__).resolve().parents[3]
        return root / "eval_sets" / f"{dataset}.jsonl"

    def build_evaluation_worker(self, dataset: str, provider: str) -> Callable[[], RunReport]:
        cases = load_dataset(self.dataset_path(dataset))
        executor = self.get_executor()
        schema_retriever = self.get_schema_retriever()
        registry = self.get_registry()
        # The console always uses admin for repeatable access semantics. The
        # selected provider is still request-local and never changes UI state.
        def agent_factory(_db_path: str | None) -> AgentLoop:
            return AgentLoop(
                llm=RoutedLLMClient(registry, selected_provider=provider),
                executor=executor,
                validator=ResultValidator(),
                schema_retriever=schema_retriever,
                enable_router=False,
                enable_summary=False,
                max_corrections=3,
            )

        def executor_factory(_db_path: str | None) -> Any:
            return executor

        return lambda: EvalHarness(
            agent_factory,
            executor_factory,
            role="admin",
        ).run(cases)

        return run

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
