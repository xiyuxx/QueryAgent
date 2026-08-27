"""结果校验：语法/执行校验 + 合理性校验（规则层）。

规则层（零额外 LLM 成本）：
1. 执行错误分类：no such column/table 等 → 可纠正，回灌给 LLM。
2. 空集判断：非聚合、非 LIMIT 查询返回 0 行 → 可疑，回灌（可能过滤过严/表列选错）。

语义级校验（结果能执行但内容错）由 agent loop 的 LLM 自审（audit）补足，见 loop.py。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..tools.db import QueryResult


@dataclass
class ValidationResult:
    ok: bool
    error: str | None = None
    correctable: bool = True


# 可纠正错误模式：属幻觉/语法问题，回灌后 LLM 有望修复
_CORRECTABLE = (
    "no such column",
    "no such table",
    "syntax error",
    "near \"",
    "unrecognized token",
    "ambiguous column",
    "misuse of aggregate",
)

_AGG = re.compile(r"\b(count|sum|avg|min|max)\s*\(", re.IGNORECASE)


def _has_aggregate(sql: str) -> bool:
    return bool(_AGG.search(sql))


class ResultValidator:
    def validate(self, sql: str, result: QueryResult) -> ValidationResult:
        if result.error is not None:
            err = result.error.lower()
            correctable = any(p in err for p in _CORRECTABLE)
            return ValidationResult(ok=False, error=result.error, correctable=correctable)

        # 合理性规则：非聚合、非 LIMIT 查询返回空集 → 可疑
        if not result.rows and not _has_aggregate(sql) and "limit" not in sql.lower():
            return ValidationResult(
                ok=False,
                error="执行结果为空（0 行），但查询非聚合/计数，可能过滤条件过严或表/列选择错误",
                correctable=True,
            )

        return ValidationResult(ok=True)
