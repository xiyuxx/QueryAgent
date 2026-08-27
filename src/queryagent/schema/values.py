"""值检索：从问题提取候选值 → 在值索引里模糊匹配 → 返回 WHERE 可用的实际值。

治两个问题：值幻觉（编造不存在的值）+ 拼写不一致（用户写「北京仓」，库里是「北京仓库」）。
"""
from __future__ import annotations

import re

from ..tools.pg import connect

from .catalog import search_values


class ValueRetriever:
    def __init__(self, dsn: str, llm=None) -> None:
        self.dsn = dsn
        self.llm = llm

    def retrieve(self, question: str, schema_context: str = "", limit: int = 5) -> list[dict]:
        candidates = self._extract_candidates(question, schema_context)
        if not candidates:
            return []
        conn = connect(self.dsn)
        try:
            matches: list[dict] = []
            seen: set[tuple] = set()
            for c in candidates:
                for m in search_values(conn, c, limit):
                    key = (m["table_name"], m["column_name"], m["value"])
                    if key in seen:
                        continue
                    seen.add(key)
                    matches.append(m)
            return matches
        finally:
            conn.close()

    def _extract_candidates(self, question: str, schema_context: str) -> list[str]:
        if self.llm is not None:
            return self.llm.extract_values(question, schema_context)
        return [w for w in re.split(r"[，。！？；：、\s]+", question) if w]

    def format_context(self, matches: list[dict]) -> str:
        lines = [
            f"- {m['table_name']}.{m['column_name']} = '{m['value']}' (相似度 {m['similarity']})"
            for m in matches
        ]
        return "\n".join(lines)
