"""多模型对照评测：同一评测集跑 DeepSeek / Qwen / MockLLM，输出对照表。

用法：
    OPENAI_API_KEY=sk-... QWEN_API_KEY=sk-... python scripts/compare_models.py \
        --db data/warehouse.db --dataset eval_sets/warehouse.jsonl --catalog warehouse --top-k 4
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from queryagent.agent.loop import AgentLoop
from queryagent.eval.dataset import load_dataset
from queryagent.eval.harness import EvalHarness
from queryagent.eval.sample_db import build_sample_db
from queryagent.eval.warehouse_db import build_warehouse_db, warehouse_catalog
from queryagent.llm.mock import MockLLM
from queryagent.llm.openai_compat import OpenAICompatClient
from queryagent.reliability.validator import ResultValidator
from queryagent.schema.retriever import SchemaRetriever
from queryagent.tools.db import SQLiteExecutor

ROOT = Path(__file__).resolve().parents[1]

_MODELS = {
    "deepseek": ("https://api.deepseek.com", "deepseek-v4-flash", "OPENAI_API_KEY"),
    "qwen": (
        "https://ws-gm39fo0e2wutrc92.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "qwen-flash",
        "QWEN_API_KEY",
    ),
}


def build_llm(name: str, temperature: float):
    if name == "mock":
        return MockLLM()
    base, model, key_env = _MODELS[name]
    return OpenAICompatClient(
        base_url=os.environ.get("OPENAI_BASE_URL") or base,
        api_key=os.environ.get(key_env, ""),
        model=model,
        temperature=temperature,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="多模型对照评测")
    parser.add_argument("--models", default="deepseek,qwen,mock", help="逗号分隔")
    parser.add_argument("--db", default=str(ROOT / "data" / "warehouse.db"))
    parser.add_argument("--dataset", default=str(ROOT / "eval_sets" / "warehouse.jsonl"))
    parser.add_argument("--catalog", choices=["none", "warehouse"], default="warehouse")
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--temperature", type=float, default=0.0)
    args = parser.parse_args()

    if "warehouse" in args.db:
        build_warehouse_db(args.db)
    else:
        build_sample_db(args.db)
    cases = load_dataset(args.dataset)
    catalog = warehouse_catalog() if args.catalog == "warehouse" else None
    print(f"dataset: {len(cases)} cases, db: {Path(args.db).name}, top_k: {args.top_k}, repeats: {args.repeats}")

    rows = []
    for name in args.models.split(","):
        name = name.strip()
        llm = build_llm(name, args.temperature)

        def agent_factory(db_path):
            return AgentLoop(
                llm=llm,
                executor=SQLiteExecutor(db_path or args.db),
                validator=ResultValidator(),
                schema_retriever=SchemaRetriever(db_path or args.db, catalog=catalog, top_k=args.top_k),
                max_corrections=3,
            )

        def exec_factory(db_path):
            return SQLiteExecutor(db_path or args.db)

        harness = EvalHarness(agent_factory, exec_factory)
        accs, tokens, costs, latencies = [], [], [], []
        for _ in range(args.repeats):
            r = harness.run(cases)
            accs.append(r.exec_accuracy)
            tokens.append(r.avg_tokens)
            costs.append(r.total_cost_usd)
            latencies.append(r.avg_latency_ms)
        rows.append((name, statistics.fmean(accs), statistics.fmean(tokens), statistics.fmean(costs), statistics.fmean(latencies)))
        print(f"  {name}: done")

    print("\n=== 模型对照 ===")
    print(f"{'model':<10}{'exec_acc':<12}{'tokens/q':<10}{'cost/run':<12}{'latency':<10}")
    for name, acc, tok, cost, lat in rows:
        print(f"{name:<10}{acc*100:>6.1f}%   {tok:>8.1f}   ${cost:<10.6f} {lat:>7.1f}ms")


if __name__ == "__main__":
    main()
