"""Agent loop 主控：plan → generate → execute → validate → audit → correct → done。

自研循环（禁用 LangChain/LlamaIndex）。状态机 + 步数/纠正轮次上限，
错误信息回灌实现自纠正。可靠性分三层：
1. 执行校验（validator）：SQL 语法/执行错误 + 空集启发式。
2. LLM 自审（audit，可选）：结果回灌让模型自评，抓“能执行但结果错”的语义错误。
3. 自纠正：错误回灌 → 重新生成，最多 max_corrections 轮。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..llm.base import LLMClient
from ..observability.trace import Span, Trace
from ..reliability.validator import ResultValidator, ValidationResult
from ..schema.retriever import SchemaRetriever
from .candidates import MultiCandidate
from ..tools.db import QueryResult
from ..tools.protocol import QueryExecutor


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
    sql: str | None = None
    rows: list[Any] | None = None
    columns: list[str] | None = None
    steps: int = 0
    corrections: int = 0
    audits: int = 0
    error: str | None = None
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    trace: Trace = field(default_factory=Trace)


class AgentLoop:
    """编排单条查询的完整链路。"""

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
        self.value_retriever = value_retriever
        self.enable_router = enable_router
        self.multi_candidate = MultiCandidate(llm, executor) if enable_multi_candidate else None

    @staticmethod
    def _preview(qres: QueryResult, max_rows: int = 10) -> str:
        if qres.error is not None:
            return qres.error
        if not qres.rows:
            return "(空结果，0 行)"
        lines = [", ".join(qres.columns)] if qres.columns else []
        for r in qres.rows[:max_rows]:
            lines.append(str(tuple(r)))
        if len(qres.rows) > max_rows:
            lines.append(f"... 共 {len(qres.rows)} 行")
        return "\n".join(lines)

    def run(self, question: str) -> AgentResult:
        trace = Trace(question=question)
        intent = "query"
        if self.enable_router:
            try:
                intent = self.llm.classify_intent(question)
            except Exception:  # noqa: BLE001 — 分类失败按 query 处理
                intent = "query"
            trace.add(Span(step="route", output=intent))
            if intent != "query":
                return AgentResult(
                    status=StepStatus.ROUTED.value,
                    question=question,
                    intent=intent,
                    trace=trace,
                )
        feedback: list[str] = []
        corrections = 0
        audits = 0
        steps = 0
        schema_ddl = self.schema_retriever.context_for(question)
        if self.value_retriever is not None:
            matches = self.value_retriever.retrieve(question, schema_ddl)
            if matches:
                schema_ddl += "\n\n相关数据值（可用于 WHERE 条件）：\n" + self.value_retriever.format_context(matches)

        sql: str | None = None
        rows: list[Any] | None = None
        columns: list[str] | None = None
        status = StepStatus.FAILED
        error: str | None = None
        total_tokens = 0
        total_cost = 0.0

        while steps < self.max_steps:
            steps += 1

            qres: QueryResult | None = None
            # generate（LLM/网络/解析错误视为不可纠正，直接失败）
            if self.multi_candidate is not None:
                sql, qres, err = self.multi_candidate.generate_and_select(question, schema_ddl, feedback)
                if sql == "":
                    error = err or "no candidates generated"
                    status = StepStatus.FAILED
                    break
                total_tokens += self.multi_candidate.total_tokens
                total_cost += self.multi_candidate.total_cost
                trace.add(
                    Span(
                        step="generate",
                        prompt=question,
                        output=sql,
                        tokens=self.multi_candidate.total_tokens,
                        cost_usd=self.multi_candidate.total_cost,
                    )
                )
            else:
                try:
                    resp = self.llm.generate_sql(question, schema_ddl, feedback)
                except Exception as e:  # noqa: BLE001 — API/解析错误
                    error = f"llm error: {type(e).__name__}: {e}"
                    status = StepStatus.FAILED
                    break
                sql = resp.parsed.sql if resp.parsed is not None else ""
                total_tokens += resp.usage.total_tokens
                total_cost += resp.usage.cost_usd
                trace.add(
                    Span(
                        step="generate",
                        prompt=question,
                        output=sql,
                        tokens=resp.usage.total_tokens,
                        cost_usd=resp.usage.cost_usd,
                        latency_ms=resp.latency_ms,
                    )
                )

            # execute（多候选已在选择阶段执行，这里复用结果）
            if qres is None:
                qres = self.executor.execute(sql)
                trace.add(Span(step="execute", output=qres.error or f"{len(qres.rows)} rows"))
            else:
                trace.add(Span(step="execute", output=f"selected via multi-candidate ({len(qres.rows)} rows)"))

            # validate（规则层：执行错误 + 空集）
            vres: ValidationResult = self.validator.validate(sql, qres)
            trace.add(Span(step="validate", output=("ok" if vres.ok else vres.error)))
            if not vres.ok:
                error = vres.error
                if not vres.correctable or corrections >= self.max_corrections:
                    status = StepStatus.FAILED
                    break
                corrections += 1
                feedback.append(error)
                trace.add(Span(step="correcting", output=error))
                continue

            # audit（LLM 自审：语义级校验）
            if self.enable_audit:
                audits += 1
                preview = self._preview(qres)
                try:
                    ares = self.llm.audit(question, sql, preview)
                except Exception as e:  # noqa: BLE001 — 审计失败视为通过，避免误伤
                    trace.add(Span(step="audit", output=f"error: {type(e).__name__}: {e}"))
                else:
                    total_tokens += ares.usage.total_tokens
                    total_cost += ares.usage.cost_usd
                    aok = ares.parsed.ok if ares.parsed is not None else True
                    trace.add(
                        Span(
                            step="audit",
                            output=("ok" if aok else ares.parsed.reason),
                            tokens=ares.usage.total_tokens,
                            cost_usd=ares.usage.cost_usd,
                            latency_ms=ares.latency_ms,
                        )
                    )
                    if ares.parsed is not None and not ares.parsed.ok:
                        error = ares.parsed.reason or "result incorrect"
                        if corrections >= self.max_corrections:
                            status = StepStatus.FAILED
                            break
                        corrections += 1
                        feedback.append("结果校验不通过：" + error)
                        trace.add(Span(step="correcting", output=error))
                        continue

            rows = qres.rows
            columns = qres.columns
            status = StepStatus.DONE
            error = None
            break

        return AgentResult(
            status=status.value,
            question=question,
            intent=intent,
            sql=sql,
            rows=rows,
            columns=columns,
            steps=steps,
            corrections=corrections,
            audits=audits,
            error=error,
            total_tokens=total_tokens,
            total_cost_usd=total_cost,
            trace=trace,
        )
