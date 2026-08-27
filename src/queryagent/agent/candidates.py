"""多候选 SQL 生成 + 选择。

生成阶段用多种推理策略（标准 / 分治 / 执行计划）产出风格各异的候选，
执行后在只读库上比较结果，用「结果一致性投票」选最优——CHASE-SQL 表明
单次生成 63% → 一致性投票 68.8%（上界 82.8%，更优的 pairwise 选择器为后续扩展）。
"""
from __future__ import annotations

from collections import Counter


class MultiCandidate:
    def __init__(self, llm, executor, strategies=("standard", "divide", "plan")) -> None:
        self.llm = llm
        self.executor = executor
        self.strategies = strategies
        self.total_tokens = 0
        self.total_cost = 0.0

    def generate_and_select(self, question: str, schema_ddl: str, feedback: list[str]):
        """生成多候选 → 执行 → 结果一致性选择，返回 (最优 SQL, 执行结果, 错误)。"""
        self.total_tokens = 0
        self.total_cost = 0.0
        candidates = self._generate(question, schema_ddl, feedback)
        if not candidates:
            return "", None, "no candidates generated"
        results = [self.executor.execute(sql) for sql in candidates]

        # 结果一致性投票
        norms = [self._normalize(r) for r in results]
        winner, _count = Counter(norms).most_common(1)[0]
        for sql, r, n in zip(candidates, results, norms):
            if n == winner:
                return sql, r, None
        return candidates[0], results[0], None

    def _generate(self, question: str, schema_ddl: str, feedback: list[str]) -> list[str]:
        candidates: list[str] = []
        for strategy in self.strategies:
            try:
                resp = self.llm.generate_sql(question, schema_ddl, feedback, strategy=strategy)
            except Exception:
                continue
            self.total_tokens += resp.usage.total_tokens
            self.total_cost += resp.usage.cost_usd
            sql = resp.parsed.sql if resp.parsed is not None else ""
            if sql and sql not in candidates:
                candidates.append(sql)
        return candidates

    @staticmethod
    def _normalize(qres) -> tuple:
        if qres.error is not None:
            return ("ERROR", qres.error)
        return ("OK", tuple(sorted(tuple(str(v) for v in row) for row in qres.rows)))
