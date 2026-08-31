"""自研 Agent loop：route -> schema -> generate -> execute -> validate -> correct。

The loop stays synchronous so it can be used by the offline evaluator. The
FastAPI layer runs it in a worker thread and forwards the callback events as
SSE without moving orchestration into a framework.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Sequence

from ..llm.base import LLMClient
from ..observability.trace import Span, Trace
from ..reliability.validator import ResultValidator, ValidationResult
from ..schema.retriever import SchemaRetriever
from ..tools.db import QueryResult
from ..tools.protocol import QueryExecutor
from .candidates import MultiCandidate


class StepStatus(str, Enum):
    PLANNING = "planning"
    GENERATING = "generating"
    EXECUTING = "executing"
    VALIDATING = "validating"
    AUDITING = "auditing"
    CORRECTING = "correcting"
    ROUTED = "routed"
    DONE = "done"
    FAILED = "failed"


@dataclass
class AgentResult:
    status: str
    question: str
    intent: str = "query"
    role: str = "readonly"
    sql: str | None = None
    rows: list[Any] | None = None
    columns: list[str] | None = None
    answer: str | None = None
    steps: int = 0
    corrections: int = 0
    audits: int = 0
    history_turns: int = 0
    schema_tables: list[str] = field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    summary_fallback: bool = False
    error: str | None = None
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    trace: Trace = field(default_factory=Trace)


EventCallback = Callable[[str, dict[str, Any]], None]


class AgentLoop:
    """编排单条查询，并通过可选 callback 暴露可观测阶段。"""

    def __init__(
        self,
        llm: LLMClient,
        executor: QueryExecutor,
        validator: ResultValidator,
        schema_retriever: SchemaRetriever,
        *,
        max_corrections: int = 3,
        max_steps: int = 5,
        enable_audit: bool = False,
        enable_router: bool = False,
        enable_summary: bool = False,
        enable_multi_candidate: bool = False,
        value_retriever=None,
    ) -> None:
        self.llm = llm
        self.executor = executor
        self.validator = validator
        self.schema_retriever = schema_retriever
        self.max_corrections = max_corrections
        self.max_steps = max_steps
        self.enable_audit = enable_audit
        self.enable_router = enable_router
        self.enable_summary = enable_summary
        self.value_retriever = value_retriever
        self.multi_candidate = MultiCandidate(llm, executor) if enable_multi_candidate else None

    @staticmethod
    def _preview(qres: QueryResult, max_rows: int = 10) -> str:
        if qres.error is not None:
            return qres.error
        if not qres.rows:
            return "(空结果，0 行)"
        lines = [", ".join(qres.columns)] if qres.columns else []
        lines.extend(str(tuple(row)) for row in qres.rows[:max_rows])
        if len(qres.rows) > max_rows:
            lines.append(f"... 共 {len(qres.rows)} 行")
        return "\n".join(lines)

    @staticmethod
    def _history_for_role(history: Sequence[dict] | None, role: str) -> list[dict]:
        """Only send the current role's latest five local turns to the model."""
        if not history:
            return []
        normalized_role = role.lower()
        matching = [
            item
            for item in history
            if str(item.get("access_role") or item.get("role", "")).lower()
            == normalized_role
        ]
        return matching[-5:]

    @staticmethod
    def _provider_info(llm: LLMClient) -> tuple[str | None, str | None]:
        provider = getattr(llm, "last_provider", None)
        if provider is None:
            return None, getattr(llm, "model", None)
        return getattr(provider, "name", None), getattr(provider, "model", None)

    @staticmethod
    def _emit(callback: EventCallback | None, event: str, **payload: Any) -> None:
        if callback is not None:
            callback(event, payload)

    @staticmethod
    def _chunks(text: str, chunk_size: int = 24) -> list[str]:
        if not text:
            return []
        return [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)]

    @staticmethod
    def _fallback_summary(qres: QueryResult) -> str:
        if qres.error:
            return "查询执行失败。"
        if not qres.rows:
            return "查询完成，结果为空。"
        if len(qres.rows) == 1 and len(qres.rows[0]) == 1:
            return f"查询完成，结果为 {qres.rows[0][0]}。"
        return f"查询完成，共返回 {len(qres.rows)} 行。"

    @staticmethod
    def result_to_dict(result: AgentResult) -> dict[str, Any]:
        """Return the frontend-safe structured result (no full prompt)."""
        return {
            "status": result.status,
            "question": result.question,
            "intent": result.intent,
            "role": result.role,
            "answer": result.answer,
            "sql": result.sql,
            "columns": result.columns,
            "rows": result.rows,
            "steps": result.steps,
            "corrections": result.corrections,
            "audits": result.audits,
            "history_turns": result.history_turns,
            "schema_tables": result.schema_tables,
            "provider": result.provider,
            "model": result.model,
            "summary_fallback": result.summary_fallback,
            "error": result.error,
            "total_tokens": result.total_tokens,
            "total_cost_usd": result.total_cost_usd,
            "trace": result.trace.to_dict(),
        }

    def _schema_context(self, question: str, role: str) -> tuple[str, list[str]]:
        try:
            context = self.schema_retriever.context_for(question, role=role)
        except TypeError:
            # Historical SQLite retriever does not accept a role. The Web Demo
            # uses MCPSchemaRetriever, which does enforce it server-side.
            context = self.schema_retriever.context_for(question)
        return context, list(getattr(self.schema_retriever, "last_tables", []))

    def _execute(self, sql: str, role: str) -> QueryResult:
        try:
            return self.executor.execute(sql, role=role)
        except TypeError:
            # Keep the old QueryExecutor protocol usable in offline tests.
            return self.executor.execute(sql)

    def _finish(
        self,
        result: AgentResult,
        callback: EventCallback | None,
    ) -> AgentResult:
        self._emit(callback, "result", result=self.result_to_dict(result))
        self._emit(callback, "done", status=result.status)
        return result

    def run(
        self,
        question: str,
        *,
        role: str = "readonly",
        history: Sequence[dict] | None = None,
        event_callback: EventCallback | None = None,
    ) -> AgentResult:
        trace = Trace(question=question)
        role_history = self._history_for_role(history, role)
        intent = "query"
        schema_tables: list[str] = []
        total_tokens = 0
        total_cost = 0.0

        self._emit(event_callback, "stage", stage="route", status="running")
        try:
            if self.enable_router:
                intent = self.llm.classify_intent(question)
            if intent not in {"query", "metadata", "chat"}:
                intent = "query"
            trace.add(Span(step="route", output=intent))
        except Exception as exc:  # a failed classifier falls back to data query
            intent = "query"
            trace.add(Span(step="route", output=f"fallback: {type(exc).__name__}: {exc}"))
        self._emit(event_callback, "stage", stage="route", status="done", intent=intent)

        # Schema questions and ordinary chat both produce text, but only the
        # schema branch calls MCP to obtain the current role-visible DDL.
        if intent in {"metadata", "chat"}:
            context = ""
            if intent == "metadata":
                self._emit(event_callback, "stage", stage="schema", status="running")
                try:
                    context, schema_tables = self._schema_context(question, role)
                except Exception as exc:
                    error = f"schema error: {type(exc).__name__}: {exc}"
                    self._emit(event_callback, "error", code="SCHEMA_UNAVAILABLE", error=error)
                    return self._finish(
                        AgentResult(
                            status=StepStatus.FAILED.value,
                            question=question,
                            intent=intent,
                            role=role,
                            history_turns=len(role_history),
                            schema_tables=schema_tables,
                            error=error,
                            trace=trace,
                        ),
                        event_callback,
                    )
                self._emit(event_callback, "stage", stage="schema", status="done", tables=schema_tables)

            self._emit(event_callback, "stage", stage="answer", status="running")
            try:
                response = self.llm.answer_text(
                    question,
                    context=context,
                    history=role_history,
                )
                answer = str(
                    getattr(response.parsed, "answer", "") if response.parsed else response.content
                )
                total_tokens = response.usage.total_tokens
                total_cost = response.usage.cost_usd
                for chunk in self._chunks(answer):
                    self._emit(event_callback, "token", text=chunk)
            except Exception as exc:
                error = f"answer error: {type(exc).__name__}: {exc}"
                self._emit(event_callback, "error", code="LLM_ERROR", error=error)
                return self._finish(
                    AgentResult(
                        status=StepStatus.FAILED.value,
                        question=question,
                        intent=intent,
                        role=role,
                        history_turns=len(role_history),
                        schema_tables=schema_tables,
                        error=error,
                        total_tokens=total_tokens,
                        total_cost_usd=total_cost,
                        trace=trace,
                    ),
                    event_callback,
                )
            self._emit(event_callback, "stage", stage="answer", status="done")
            provider, model = self._provider_info(self.llm)
            return self._finish(
                AgentResult(
                    status=StepStatus.DONE.value,
                    question=question,
                    intent=intent,
                    role=role,
                    answer=answer,
                    steps=1,
                    history_turns=len(role_history),
                    schema_tables=schema_tables,
                    provider=provider,
                    model=model,
                    total_tokens=total_tokens,
                    total_cost_usd=total_cost,
                    trace=trace,
                ),
                event_callback,
            )

        self._emit(event_callback, "stage", stage="schema", status="running")
        try:
            schema_ddl, schema_tables = self._schema_context(question, role)
        except Exception as exc:
            error = f"schema error: {type(exc).__name__}: {exc}"
            self._emit(event_callback, "error", code="SCHEMA_UNAVAILABLE", error=error)
            return self._finish(
                AgentResult(
                    status=StepStatus.FAILED.value,
                    question=question,
                    intent=intent,
                    role=role,
                    history_turns=len(role_history),
                    error=error,
                    trace=trace,
                ),
                event_callback,
            )
        self._emit(event_callback, "stage", stage="schema", status="done", tables=schema_tables)

        feedback: list[str] = []
        corrections = 0
        audits = 0
        steps = 0
        sql: str | None = None
        rows: list[Any] | None = None
        columns: list[str] | None = None
        answer: str | None = None
        summary_fallback = False
        status = StepStatus.FAILED
        error: str | None = None

        while steps < self.max_steps:
            steps += 1
            qres: QueryResult | None = None
            self._emit(event_callback, "stage", stage="generate", status="running", step=steps)
            if self.multi_candidate is not None:
                try:
                    sql, qres, candidate_error = self.multi_candidate.generate_and_select(
                        question, schema_ddl, feedback
                    )
                except Exception as exc:
                    error = f"llm error: {type(exc).__name__}: {exc}"
                    self._emit(event_callback, "error", code="LLM_ERROR", error=error)
                    break
                if sql == "":
                    error = candidate_error or "no candidates generated"
                    break
                total_tokens += self.multi_candidate.total_tokens
                total_cost += self.multi_candidate.total_cost
            else:
                try:
                    response = self.llm.generate_sql(
                        question,
                        schema_ddl,
                        feedback,
                        history=role_history,
                    )
                except Exception as exc:
                    error = f"llm error: {type(exc).__name__}: {exc}"
                    self._emit(event_callback, "error", code="LLM_ERROR", error=error)
                    break
                sql = response.parsed.sql if response.parsed is not None else ""
                total_tokens += response.usage.total_tokens
                total_cost += response.usage.cost_usd
                trace.add(
                    Span(
                        step="generate",
                        prompt=question,
                        output=sql,
                        tokens=response.usage.total_tokens,
                        cost_usd=response.usage.cost_usd,
                        latency_ms=response.latency_ms,
                    )
                )
            self._emit(event_callback, "stage", stage="generate", status="done", sql=sql or "")

            self._emit(event_callback, "stage", stage="execute", status="running")
            if qres is None:
                qres = self._execute(sql or "", role)
            trace.add(Span(step="execute", output=qres.error or f"{len(qres.rows)} rows"))
            self._emit(
                event_callback,
                "stage",
                stage="execute",
                status="done" if qres.error is None else "error",
                rows=len(qres.rows),
                error=qres.error,
            )

            self._emit(event_callback, "stage", stage="validate", status="running")
            validation: ValidationResult = self.validator.validate(sql or "", qres)
            trace.add(Span(step="validate", output="ok" if validation.ok else validation.error))
            self._emit(
                event_callback,
                "stage",
                stage="validate",
                status="done" if validation.ok else "error",
                error=validation.error,
            )
            if not validation.ok:
                error = validation.error
                if not validation.correctable or corrections >= self.max_corrections:
                    status = StepStatus.FAILED
                    break
                corrections += 1
                feedback.append(error or "unknown validation error")
                trace.add(Span(step="correcting", output=error))
                self._emit(
                    event_callback,
                    "stage",
                    stage="correct",
                    status="running",
                    error=error,
                    correction=corrections,
                )
                self._emit(
                    event_callback,
                    "stage",
                    stage="correct",
                    status="done",
                    correction=corrections,
                )
                continue

            if self.enable_audit:
                audits += 1
                self._emit(event_callback, "stage", stage="audit", status="running")
                try:
                    audit = self.llm.audit(question, sql or "", self._preview(qres))
                    total_tokens += audit.usage.total_tokens
                    total_cost += audit.usage.cost_usd
                    audit_ok = audit.parsed.ok if audit.parsed is not None else True
                    audit_reason = audit.parsed.reason if audit.parsed is not None else ""
                    trace.add(Span(step="audit", output="ok" if audit_ok else audit_reason))
                    self._emit(
                        event_callback,
                        "stage",
                        stage="audit",
                        status="done" if audit_ok else "error",
                        error=audit_reason or None,
                    )
                except Exception as exc:
                    # Audit is a secondary guard. An unavailable audit model
                    # must not discard an already successful read-only query.
                    trace.add(Span(step="audit", output=f"error: {type(exc).__name__}: {exc}"))
                    self._emit(event_callback, "stage", stage="audit", status="fallback", error=str(exc))
                    audit_ok = True
                    audit_reason = ""
                if not audit_ok:
                    error = audit_reason or "result incorrect"
                    if corrections >= self.max_corrections:
                        status = StepStatus.FAILED
                        break
                    corrections += 1
                    feedback.append("结果校验不通过：" + error)
                    trace.add(Span(step="correcting", output=error))
                    self._emit(event_callback, "stage", stage="correct", status="running", error=error, correction=corrections)
                    self._emit(event_callback, "stage", stage="correct", status="done", correction=corrections)
                    continue

            rows = qres.rows
            columns = qres.columns
            status = StepStatus.DONE
            error = None
            if self.enable_summary:
                self._emit(event_callback, "stage", stage="summary", status="running")
                try:
                    summary = self.llm.summarize_result(question, sql or "", qres.columns, qres.rows)
                    answer = str(
                        getattr(summary.parsed, "answer", "")
                        if summary.parsed
                        else summary.content
                    )
                    total_tokens += summary.usage.total_tokens
                    total_cost += summary.usage.cost_usd
                    for chunk in self._chunks(answer):
                        self._emit(event_callback, "token", text=chunk)
                    self._emit(event_callback, "stage", stage="summary", status="done")
                except Exception as exc:
                    summary_fallback = True
                    answer = self._fallback_summary(qres)
                    self._emit(event_callback, "token", text=answer)
                    self._emit(event_callback, "stage", stage="summary", status="fallback", error=str(exc))
            break

        provider, model = self._provider_info(self.llm)
        result = AgentResult(
            status=status.value,
            question=question,
            intent=intent,
            role=role,
            sql=sql,
            rows=rows,
            columns=columns,
            answer=answer,
            steps=steps,
            corrections=corrections,
            audits=audits,
            history_turns=len(role_history),
            schema_tables=schema_tables,
            provider=provider,
            model=model,
            summary_fallback=summary_fallback,
            error=error,
            total_tokens=total_tokens,
            total_cost_usd=total_cost,
            trace=trace,
        )
        return self._finish(result, event_callback)
