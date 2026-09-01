"""指标定义与聚合：execution accuracy、exact match、平均步数、token、成本、延迟。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CaseMetrics:
    case_id: str
    status: str
    question: str = ""
    sql: str = ""
    gold_sql: str = ""
    exec_match: bool | None = None
    exact_match: bool = False
    steps: int = 0
    corrections: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    error: str | None = None


@dataclass
class RunReport:
    cases: list[CaseMetrics] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "executed": self.executed,
            "exec_accuracy": self.exec_accuracy,
            "exact_match_rate": self.exact_match_rate,
            "avg_steps": self.avg_steps,
            "avg_corrections": self.avg_corrections,
            "avg_tokens": self.avg_tokens,
            "total_cost_usd": self.total_cost_usd,
            "avg_latency_ms": self.avg_latency_ms,
            "cases": [case.__dict__ for case in self.cases],
        }

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def executed(self) -> int:
        return sum(1 for c in self.cases if c.exec_match is not None)

    @property
    def exec_accuracy(self) -> float:
        evaluated = [c for c in self.cases if c.exec_match is not None]
        if not evaluated:
            return 0.0
        return sum(1 for c in evaluated if c.exec_match) / len(evaluated)

    @property
    def exact_match_rate(self) -> float:
        if not self.cases:
            return 0.0
        return sum(1 for c in self.cases if c.exact_match) / len(self.cases)

    @property
    def avg_steps(self) -> float:
        return sum(c.steps for c in self.cases) / len(self.cases) if self.cases else 0.0

    @property
    def avg_corrections(self) -> float:
        return sum(c.corrections for c in self.cases) / len(self.cases) if self.cases else 0.0

    @property
    def avg_tokens(self) -> float:
        return sum(c.tokens for c in self.cases) / len(self.cases) if self.cases else 0.0

    @property
    def total_cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.cases)

    @property
    def avg_latency_ms(self) -> float:
        return sum(c.latency_ms for c in self.cases) / len(self.cases) if self.cases else 0.0
