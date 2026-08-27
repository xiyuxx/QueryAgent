"""轻量 span 记录：定位“哪一步最常失败、哪步最贵”。

Langfuse / OpenTelemetry 为后续可选项；当前自建内存 span，评测后聚合。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Span:
    step: str
    prompt: str | None = None
    output: str | None = None
    tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "prompt": self.prompt,
            "output": self.output,
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
        }


@dataclass
class Trace:
    question: str = ""
    spans: list[Span] = field(default_factory=list)

    def add(self, span: Span) -> None:
        self.spans.append(span)

    def to_dict(self) -> dict[str, Any]:
        return {"question": self.question, "spans": [s.to_dict() for s in self.spans]}
