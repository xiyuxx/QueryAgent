"""BIRD dev 子集加载与评测。

BIRD = 大规模真实数据库 Text-to-SQL 基准（英文）。从 dev.json + dev_databases 抽取
分层抽样子集，每条用例带 db_path 指向其 SQLite 库，供多库评测。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from .dataset import EvalCase


def load_bird_subset(
    dev_json: str | Path,
    db_root: str | Path,
    *,
    n: int = 200,
    seed: int = 42,
    with_evidence: bool = True,
) -> list[EvalCase]:
    """从 BIRD dev.json 分层抽样 n 条（跨 db 均衡），返回带 db_path 的用例。"""
    data = json.loads(Path(dev_json).read_text(encoding="utf-8"))
    rng = random.Random(seed)

    by_db: dict[str, list[dict]] = {}
    for ex in data:
        by_db.setdefault(ex["db_id"], []).append(ex)

    cases: list[EvalCase] = []
    db_ids = sorted(by_db)
    while len(cases) < n:
        progressed = False
        for db in db_ids:
            pool = by_db[db]
            if not pool:
                continue
            ex = pool.pop(rng.randrange(len(pool)))
            db_path = Path(db_root) / "dev_databases" / db / f"{db}.sqlite"
            if not db_path.exists():
                continue
            question = ex["question"]
            if with_evidence and ex.get("evidence"):
                question = f"{question}\n[外部知识提示] {ex['evidence']}"
            cases.append(
                EvalCase(
                    id=f"bird_{ex['question_id']}",
                    question=question,
                    gold_sql=ex["SQL"],
                    difficulty=ex.get("difficulty", ""),
                    domain=db,
                    db_path=str(db_path),
                )
            )
            progressed = True
            if len(cases) >= n:
                break
        if not progressed:
            break
    return cases
