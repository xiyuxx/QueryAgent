"""评测数据集：加载 JSONL 用例（id / question / gold_sql / 可选字段）。

db_path 可选：单库评测时为 None（用共享库），多库评测（如 BIRD）时指向该用例的库文件。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class EvalCase:
    id: str
    question: str
    gold_sql: str
    difficulty: str = "easy"
    domain: str = ""
    db_path: str | None = None


def load_dataset(path: str | Path) -> list[EvalCase]:
    p = Path(path)
    cases: list[EvalCase] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        obj = json.loads(line)
        cases.append(
            EvalCase(
                id=obj["id"],
                question=obj["question"],
                gold_sql=obj["gold_sql"],
                difficulty=obj.get("difficulty", "easy"),
                domain=obj.get("domain", ""),
                db_path=obj.get("db_path"),
            )
        )
    return cases
