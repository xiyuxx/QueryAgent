"""评测 harness：离线跑全链路，产出可复现指标。

对每条用例：跑 agent → 执行 gold SQL → 比较结果集（execution accuracy）
与归一化 SQL 文本（exact match）→ 汇总指标。

agent/executor 用工厂按用例的 db_path 构造，支持单库（db_path=None）与多库（BIRD）。
"""
from __future__ import annotations

import re
import time
from typing import Callable

from ..agent.loop import AgentLoop
from ..tools.protocol import QueryExecutor
from .dataset import EvalCase
from .metrics import CaseMetrics, RunReport


def normalize_sql(sql: str) -> str:
    s = re.sub(r"\s+", " ", (sql or "").strip().lower())
    s = s.replace('"', "'")
    s = re.sub(r"\s*;\s*$", "", s)
    return s


def _normalize_rows(rows: list[tuple]) -> list[tuple[str, ...]]:
    return sorted(tuple(str(v) for v in r) for r in rows)


def compare_results(agent_rows: list[tuple], gold_rows: list[tuple]) -> bool:
    return _normalize_rows(agent_rows) == _normalize_rows(gold_rows)


class EvalHarness:
    def __init__(
        self,
        agent_factory: Callable[[str | None], AgentLoop],
        executor_factory: Callable[[str | None], QueryExecutor],
        *,
        role: str = "readonly",
    ) -> None:
        self.agent_factory = agent_factory
        self.executor_factory = executor_factory
        self.role = role

    def run_case(self, case: EvalCase) -> CaseMetrics:
        t0 = time.perf_counter()
        agent = self.agent_factory(case.db_path)
        try:
            result = agent.run(case.question, role=self.role)
        finally:
            close = getattr(agent.executor, "close", None)
            if callable(close):
                close()
        latency_ms = (time.perf_counter() - t0) * 1000.0

        gold_executor = self.executor_factory(case.db_path)
        try:
            try:
                gold = gold_executor.execute(case.gold_sql, role=self.role)
            except TypeError:
                gold = gold_executor.execute(case.gold_sql)
        finally:
            close = getattr(gold_executor, "close", None)
            if callable(close):
                close()
        exec_match: bool | None = None
        if result.status == "done" and gold.error is None:
            exec_match = compare_results(result.rows or [], gold.rows)
        elif result.status != "done":
            exec_match = False

        exact = normalize_sql(result.sql or "") == normalize_sql(case.gold_sql)

        return CaseMetrics(
            case_id=case.id,
            status=result.status,
            question=case.question,
            sql=result.sql or "",
            gold_sql=case.gold_sql,
            exec_match=exec_match,
            exact_match=exact,
            steps=result.steps,
            corrections=result.corrections,
            tokens=result.total_tokens,
            cost_usd=result.total_cost_usd,
            latency_ms=latency_ms,
            error=result.error,
        )

    def run(self, cases: list[EvalCase]) -> RunReport:
        report = RunReport()
        for case in cases:
            report.cases.append(self.run_case(case))
        return report
