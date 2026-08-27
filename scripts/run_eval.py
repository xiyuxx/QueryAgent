"""评测入口：构造示例库 → 加载评测集 → 跑 agent → 输出指标表。

用法（在仓库根目录）：
    python -m scripts.run_eval                                          # 离线 MockLLM + 小库
    python -m scripts.run_eval --llm deepseek --db data/warehouse.db \
        --dataset eval_sets/warehouse.jsonl --catalog warehouse --top-k 4
    python -m scripts.run_eval --llm qwen --repeats 3

默认跑“基线（无自纠正） vs 自纠正开启”对照实验；--top-k 控制 schema 注入的相关表数量
（None = 全量注入，用于对照 schema 选择的收益）。reasoning 模型非确定，用 --repeats 报均值±方差。
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "src"))

from queryagent.agent.loop import AgentLoop
from queryagent.eval.dataset import EvalCase, load_dataset
from queryagent.eval.harness import EvalHarness
from queryagent.eval.metrics import RunReport
from queryagent.eval.sample_db import build_sample_db
from queryagent.eval.warehouse_db import build_warehouse_db, warehouse_catalog
from queryagent.llm.base import LLMClient
from queryagent.llm.mock import MockLLM
from queryagent.llm.openai_compat import OpenAICompatClient
from queryagent.reliability.validator import ResultValidator
from queryagent.schema.retriever import SchemaRetriever
from queryagent.tools.db import SQLiteExecutor
from queryagent.tools.mcp_client import MCPExecutor
from queryagent.tools.sandbox import SandboxExecutor
DEFAULT_DEEPSEEK_BASE = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_QWEN_BASE = "https://ws-gm39fo0e2wutrc92.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
DEFAULT_QWEN_MODEL = "qwen-flash"


def build_llm(args: argparse.Namespace) -> LLMClient:
    if args.llm == "mock":
        return MockLLM()
    if args.llm == "deepseek":
        return OpenAICompatClient(
            base_url=args.base_url or os.environ.get("OPENAI_BASE_URL") or DEFAULT_DEEPSEEK_BASE,
            api_key=args.api_key or os.environ.get("OPENAI_API_KEY") or "",
            model=args.model or os.environ.get("OPENAI_MODEL") or DEFAULT_DEEPSEEK_MODEL,
            temperature=args.temperature,
        )
    if args.llm == "qwen":
        return OpenAICompatClient(
            base_url=args.base_url or os.environ.get("QWEN_BASE_URL") or DEFAULT_QWEN_BASE,
            api_key=args.api_key or os.environ.get("QWEN_API_KEY") or "",
            model=args.model or os.environ.get("QWEN_MODEL") or DEFAULT_QWEN_MODEL,
            temperature=args.temperature,
        )
    raise SystemExit(f"unknown --llm: {args.llm}")


def build_catalog(name: str | None) -> dict | None:
    if name == "warehouse":
        return warehouse_catalog()
    return None


def build_executor(mode: str, db_path: str, sandbox_backend: str):
    if mode == "sqlite":
        return SQLiteExecutor(db_path)
    if mode == "sandbox":
        return SandboxExecutor(db_path, backend=sandbox_backend)
    if mode == "mcp":
        return MCPExecutor(db_path, backend=sandbox_backend)
    raise ValueError(f"unknown executor mode: {mode}")


def build_agent(
    max_corrections: int,
    llm: LLMClient,
    db_path: str,
    catalog: dict | None,
    top_k: int | None,
    executor,
    enable_audit: bool = False,
) -> AgentLoop:
    return AgentLoop(
        llm=llm,
        executor=executor,
        validator=ResultValidator(),
        schema_retriever=SchemaRetriever(str(db_path), catalog=catalog, top_k=top_k),
        max_corrections=max_corrections,
        enable_audit=enable_audit,
    )


def mean_std(xs: list[float]) -> tuple[float, float]:
    m = statistics.fmean(xs)
    sd = statistics.pstdev(xs) if len(xs) > 1 else 0.0
    return m, sd


def run_config(agent_factory, executor_factory, cases: list[EvalCase], repeats: int):
    accuracies: list[float] = []
    per_case = {c.id: 0 for c in cases}
    tokens: list[int] = []
    costs: list[float] = []
    latencies: list[float] = []
    last_report: RunReport | None = None
    for _ in range(repeats):
        report = EvalHarness(agent_factory, executor_factory).run(cases)
        accuracies.append(report.exec_accuracy)
        for cm in report.cases:
            if cm.exec_match:
                per_case[cm.case_id] += 1
            tokens.append(cm.tokens)
            latencies.append(cm.latency_ms)
        costs.append(report.total_cost_usd)
        last_report = report
    return accuracies, per_case, tokens, costs, latencies, last_report


def format_detailed(title: str, report: RunReport) -> None:
    print(f"\n=== {title} ===")
    header = f"{'id':<5}{'status':<8}{'exec':<6}{'exact':<6}{'steps':<6}{'corr':<5}{'tokens':<8}{'cost$':<11}{'lat_ms':<9}"
    print(header)
    print("-" * len(header))
    for c in report.cases:
        em = "-" if c.exec_match is None else ("PASS" if c.exec_match else "FAIL")
        print(
            f"{c.case_id:<5}{c.status:<8}{em:<6}{('yes' if c.exact_match else 'no'):<6}"
            f"{c.steps:<6}{c.corrections:<5}{c.tokens:<8}{c.cost_usd:<11.5f}{c.latency_ms:<9.1f}"
        )
    print("-" * len(header))


def print_config(
    name: str,
    accuracies: list[float],
    per_case: dict[str, int],
    tokens: list[int],
    costs: list[float],
    latencies: list[float],
    last_report: RunReport,
) -> None:
    n = len(accuracies)
    m, sd = mean_std(accuracies)
    if n == 1:
        format_detailed(name, last_report)
    else:
        print(f"\n=== {name} ===")
        print(f"{'id':<6}{'pass':<10}")
        for cid, p in per_case.items():
            print(f"{cid:<6}{p}/{n}")
    print(f"execution accuracy : {m * 100:.1f}% ± {sd * 100:.1f}pp  ({n} run{'s' if n > 1 else ''})")
    print(f"avg tokens / query : {statistics.fmean(tokens):.1f}")
    print(f"avg cost / run     : ${statistics.fmean(costs):.6f}")
    print(f"avg latency        : {statistics.fmean(latencies):.1f} ms")


def main() -> None:
    parser = argparse.ArgumentParser(description="QueryAgent 评测")
    parser.add_argument("--llm", choices=["mock", "deepseek", "qwen"], default="mock")
    parser.add_argument("--model", help="模型名")
    parser.add_argument("--base-url", help="API base URL")
    parser.add_argument("--api-key", help="API key（优先用环境变量）")
    parser.add_argument("--db", default=str(ROOT / "data" / "sales.db"), help="SQLite 库路径")
    parser.add_argument("--dataset", default=str(ROOT / "eval_sets" / "mini.jsonl"), help="评测集 JSONL")
    parser.add_argument("--catalog", choices=["none", "warehouse"], default="none", help="schema 目录（描述语料）")
    parser.add_argument("--top-k", type=int, default=None, help="注入的相关表数量（None=全量）")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--audit", action="store_true", help="开启 LLM 结果自审（语义校验）")
    parser.add_argument("--executor", choices=["mcp", "sandbox", "sqlite"], default="mcp", help="SQL 执行边界（默认 mcp）")
    parser.add_argument("--sandbox-backend", choices=["subprocess", "docker"], default="subprocess", help="mcp/sandbox 的隔离后端")
    args = parser.parse_args()

    # 建库（幂等）
    if "warehouse" in args.db:
        build_warehouse_db(args.db)
    else:
        build_sample_db(args.db)

    cases = load_dataset(args.dataset)
    llm = build_llm(args)
    catalog = build_catalog(args.catalog)

    label = args.llm
    if args.llm != "mock":
        label = f"{args.llm} ({llm.model}, T={llm.temperature})"
        print(f"cost model: input ${llm.input_price_per_1k:.6f}/1k, output ${llm.output_price_per_1k:.6f}/1k (estimate)")
    print(
        f"llm: {label}, dataset: {len(cases)} cases, db: {Path(args.db).name}, "
        f"top_k: {args.top_k}, executor: {args.executor}/{args.sandbox_backend}, repeats: {args.repeats}"
    )

    def make_factory(corr: int):
        def factory(db_path: str | None) -> AgentLoop:
            resolved_path = db_path or args.db
            executor = build_executor(args.executor, resolved_path, args.sandbox_backend)
            return build_agent(corr, llm, resolved_path, catalog, args.top_k, executor, args.audit)
        return factory

    def exec_factory(db_path: str | None) -> SQLiteExecutor:
        return SQLiteExecutor(db_path or args.db)

    base = run_config(make_factory(0), exec_factory, cases, args.repeats)
    print_config("基线（无自纠正）", *base)

    corr = run_config(make_factory(3), exec_factory, cases, args.repeats)
    print_config("自纠正开启（≤3 轮）", *corr)

    mb, sb = mean_std(base[0])
    mc, sc = mean_std(corr[0])
    print(
        "\n对照结果：execution accuracy "
        f"{mb * 100:.1f}% ± {sb * 100:.1f}pp → {mc * 100:.1f}% ± {sc * 100:.1f}pp "
        f"(Δ {(mc - mb) * 100:+.1f}pp)"
    )
    print(
        f"成本代价：avg cost / run ${statistics.fmean(base[3]):.6f} → "
        f"${statistics.fmean(corr[3]):.6f}"
    )


if __name__ == "__main__":
    main()
