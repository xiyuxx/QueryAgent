"""Schema 感知与上下文管理（三路检索）。

- 稠密检索：embedding（bge-small-zh）余弦相似度。
- 稀疏检索：BM25（字符 bigram 分词）。
- 值检索：pg_trgm 模糊匹配（见 values.py）。

PG 后端用 pgvector 离线索引（预计算表向量，查询只嵌入问题）；SQLite 后端现算。
两路结果用 RRF 融合。样本行注入防值幻觉，token 预算截断防上下文腐化。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from .bm25 import BM25

_embedder = None


def _get_embedder():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding

        _embedder = TextEmbedding(model_name="BAAI/bge-small-zh-v1.5")
    return _embedder


def _cosine(a, b) -> float:
    import numpy as np

    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 3)


@dataclass
class Column:
    name: str
    type: str


@dataclass
class Table:
    name: str
    columns: list[Column] = field(default_factory=list)


class SchemaRetriever:
    """SQLite 后端：BM25 + 现算 embedding，RRF 融合。"""

    def __init__(
        self,
        db_path: str,
        sample_rows: int = 3,
        token_budget: int = 1500,
        catalog: Optional[dict] = None,
        top_k: Optional[int] = None,
    ) -> None:
        self.db_path = db_path
        self.max_sample_rows = sample_rows
        self.token_budget = token_budget
        self.catalog = catalog or {}
        self.top_k = top_k
        self.tables = self._load_tables()
        self._bm25 = BM25([self._table_doc(t) for t in self.tables])

    def _load_tables(self) -> list[Table]:
        conn = sqlite3.connect(self.db_path)
        try:
            names = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            tables = []
            for name in names:
                cols = [
                    Column(c[1], c[2]) for c in conn.execute(f'PRAGMA table_info("{name}")')
                ]
                tables.append(Table(name=name, columns=cols))
            return tables
        finally:
            conn.close()

    def full_ddl(self) -> str:
        return "\n".join(self._ddl(t) for t in self.tables)

    def _ddl(self, t: Table) -> str:
        cols = ", ".join(f"{c.name} {c.type}" for c in t.columns)
        return f"CREATE TABLE {t.name} ({cols});"

    def sample_rows(self, table: str) -> list[tuple]:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                f'SELECT * FROM "{table}" LIMIT {self.max_sample_rows}'
            ).fetchall()
        finally:
            conn.close()

    def _table_doc(self, t: Table) -> str:
        cat = self.catalog.get(t.name, {})
        parts = [cat.get("description", ""), t.name]
        colmap = cat.get("columns", {})
        for c in t.columns:
            parts.append(c.name)
            if c.name in colmap:
                parts.append(colmap[c.name])
        return " ".join(parts)

    # ---- 三路检索 ----
    def _embedding_order(self, question: str) -> list[Table]:
        q_emb = list(_get_embedder().embed([question]))[0]
        docs = [self._table_doc(t) for t in self.tables]
        doc_embs = list(_get_embedder().embed(docs))
        scored = [( _cosine(q_emb, de), t) for t, de in zip(self.tables, doc_embs)]
        scored.sort(key=lambda x: -x[0])
        return [t for _, t in scored]

    def _bm25_order(self, question: str) -> list[Table]:
        scores = self._bm25.scores(question)
        ranked = sorted(zip(self.tables, scores), key=lambda x: -x[1])
        return [t for t, _ in ranked]

    def _relevance_order(self, question: str) -> list[Table]:
        if self.top_k is None and len(self.tables) <= 6:
            # 小库不截断：BM25 足够，省去 embedding 加载
            return self._bm25_order(question)
        return self._rrf([self._embedding_order(question), self._bm25_order(question)])

    @staticmethod
    def _rrf(ranked_lists: list[list[Table]], k: int = 60) -> list[Table]:
        scores: dict[str, float] = {}
        for lst in ranked_lists:
            for rank, t in enumerate(lst):
                scores[t.name] = scores.get(t.name, 0.0) + 1.0 / (k + rank + 1)
        ordered = sorted(ranked_lists[0], key=lambda t: -scores.get(t.name, 0.0))
        return ordered

    def context_for(self, question: str) -> str:
        ordered = self._relevance_order(question)
        selected = ordered[: self.top_k] if self.top_k else ordered
        parts: list[str] = []
        used = 0
        for t in selected:
            ddl = self._ddl(t)
            parts.append(ddl)
            used += _estimate_tokens(ddl)
            for r in self.sample_rows(t.name):
                line = f"-- {t.name} 示例行: {tuple(r)}"
                cost = _estimate_tokens(line)
                if used + cost > self.token_budget:
                    break
                parts.append(line)
                used += cost
        return "\n".join(parts)


def load_schema_from_pg(dsn: str, db_name: str = "warehouse") -> tuple[list[Table], dict]:
    """从 PostgreSQL 加载表结构（正确列顺序）+ 元数据目录描述。"""
    from ..tools.pg import connect

    from .catalog import fetch_metadata

    conn = connect(dsn)
    try:
        cur = conn.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name <> 'schema_metadata' "
            "AND table_name <> 'value_index' AND table_name <> 'table_embeddings' "
            "ORDER BY table_name, ordinal_position"
        )
        table_cols: dict[str, list[tuple[str, str]]] = {}
        for tn, cn, dt in cur.fetchall():
            table_cols.setdefault(tn, []).append((cn, dt))
        meta = fetch_metadata(conn, db_name)
    finally:
        conn.close()

    catalog: dict = {}
    for m in meta:
        cat = catalog.setdefault(
            m["table_name"], {"description": m["table_description"], "columns": {}}
        )
        cat["columns"][m["column_name"]] = m["column_description"]
    tables = [
        Table(name=tn, columns=[Column(cn, dt) for cn, dt in cols])
        for tn, cols in table_cols.items()
    ]
    return tables, catalog


class PgSchemaRetriever(SchemaRetriever):
    """PostgreSQL 后端：BM25 + pgvector 离线索引，复用 SchemaRetriever 的检索/上下文逻辑。"""

    def __init__(
        self,
        dsn: str,
        *,
        sample_rows: int = 3,
        token_budget: int = 1500,
        top_k: Optional[int] = None,
        db_name: str = "warehouse",
    ) -> None:
        self._dsn = dsn
        self._db_name = db_name
        self.max_sample_rows = sample_rows
        self.token_budget = token_budget
        self.top_k = top_k
        self.tables, self.catalog = load_schema_from_pg(dsn, db_name)
        self._bm25 = BM25([self._table_doc(t) for t in self.tables])
        self._seed_embeddings()

    def _seed_embeddings(self) -> None:
        from ..tools.pg import connect

        from .catalog import seed_table_embeddings

        conn = connect(self._dsn)
        try:
            docs = [(t.name, self._table_doc(t)) for t in self.tables]
            seed_table_embeddings(
                conn, self._db_name, docs,
                lambda d: list(_get_embedder().embed([d]))[0],
            )
        finally:
            conn.close()

    def _embedding_order(self, question: str) -> list[Table]:
        from ..tools.pg import connect

        from .catalog import search_embeddings

        q_vec = list(_get_embedder().embed([question]))[0]
        conn = connect(self._dsn)
        try:
            results = search_embeddings(conn, self._db_name, q_vec, len(self.tables))
        finally:
            conn.close()
        by_name = {t.name: t for t in self.tables}
        return [by_name[tn] for tn, _ in results if tn in by_name]

    def sample_rows(self, table: str) -> list[tuple]:
        from ..tools.pg import connect

        conn = connect(self._dsn)
        try:
            cur = conn.execute(f'SELECT * FROM "{table}" LIMIT {self.max_sample_rows}')
            return cur.fetchall()
        finally:
            conn.close()
