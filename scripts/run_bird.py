"""BIRD dev 子集评测（英文、多库、真实 schema）。

用法：
    OPENAI_API_KEY=sk-... python scripts/run_bird.py --llm deepseek --n 50
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from queryagent.agent.loop import AgentLoop
from queryagent.eval.bird import load_bird_subset
from queryagent.eval.harness import EvalHarness
from queryagent.eval.metrics import RunReport
from queryagent.llm.mock import MockLLM
from queryagent.llm.openai_compat import OpenAICompatClient
from queryagent.reliability.validator import ResultValidator
from queryagent.schema.retriever import SchemaRetriever
from queryagent.tools.db import SQLiteExecutor

ROOT = Path(__file__).resolve().parents[1]
DEV_JSON = ROOT / "data" / "bird" / "dev_20240627" / "dev.json"
DB_ROOT = ROOT / "data" / "bird_databases"

DEFAULT_DEEPSEEK_BASE = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_QWEN_BASE = "https://ws-gm39fo0e2wutrc92.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
DEFAULT_QWEN_MODEL = "qwen-flash"


def build_llm(args: argparse.Namespace):
    if args.llm == "mock":
        return MockLLM()
    if args.llm == "deepseek":
        return OpenAICompatClient(
            base_url=os.environ.get("OPENAI_BASE_URL") or DEFAULT_DEEPSEEK_BASE,
            api_key=os.environ.get("OPENAI_API_KEY") or "",
            model=args.model or os.environ.get("OPENAI_MODEL") or DEFAULT_DEEPSEEK_MODEL,
            temperature=args.temperature,
        )
    if args.llm == "qwen":
        return OpenAICompatClient(
            base_url=os.environ.get("QWEN_BASE_URL") or DEFAULT_QWEN_BASE,
            api_key=os.environ.get("QWEN_API_KEY") or "",
            model=args.model or os.environ.get("QWEN_MODEL") or DEFAULT_QWEN_MODEL,
            temperature=args.temperature,
        )
    raise SystemExit(f"unknown --llm: {args.llm}")


def summarize(name: str, report: RunReport) -> None:
    print(f"\n=== {name} ===")
    print(f"execution accuracy : {report.exec_accuracy * 100:.1f}%  ({report.executed} evaluated)")
    print(f"exact match        : {report.exact_match_rate * 100:.1f}%")
    print(f"avg steps          : {report.avg_steps:.2f}")
    print(f"avg tokens / query : {report.avg_tokens:.1f}")
    print(f"total cost         : ${report.total_cost_usd:.6f}")
    print(f"avg latency        : {report.avg_latency_ms:.1f} ms")


def main() -> None:
    parser = argparse.ArgumentParser(description="BIRD dev 子集评测")
    parser.add_argument("--llm", choices=["mock", "deepseek", "qwen"], default="deepseek")
    parser.add_argument("--model")
    parser.add_argument("--n", type=int, default=50, help="BIRD 子集大小")
    parser.add_argument("--top-k", type=int, default=None, help="schema 注入相关表数（None=全量）")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--temperature", type=float, default=0.0)
    args = parser.parse_args()

    cases = load_bird_subset(DEV_JSON, DB_ROOT, n=args.n)
    llm = build_llm(args)
    print(f"llm: {args.llm}, BIRD cases: {len(cases)}, top_k: {args.top_k}")

    def agent_factory(db_path: str | None) -> AgentLoop:
        return AgentLoop(
            llm=llm,
            executor=SQLiteExecutor(db_path),
            validator=ResultValidator(),
            schema_retriever=SchemaRetriever(db_path, top_k=args.top_k),
            max_corrections=3,
            enable_audit=args.audit,
        )

    def exec_factory(db_path: str | None) -> SQLiteExecutor:
        return SQLiteExecutor(db_path)

    harness = EvalHarness(agent_factory, exec_factory)
    report = harness.run(cases)
    summarize("BIRD dev 子集", report)


if __name__ == "__main__":
    main()
