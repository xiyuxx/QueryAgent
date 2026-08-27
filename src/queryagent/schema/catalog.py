"""元数据目录（语义层）+ 值索引。

- schema_metadata：表/列的业务描述、术语、口径、样例值（schema 检索的「源」）。
- value_index：文本列的去重取值 + pg_trgm 索引，供「值检索」模糊匹配 WHERE 要用的实际值。
"""
from __future__ import annotations

METADATA_DDL = """
CREATE TABLE IF NOT EXISTS schema_metadata (
    db_name TEXT NOT NULL DEFAULT 'warehouse',
    table_name TEXT NOT NULL,
    table_description TEXT NOT NULL DEFAULT '',
    column_name TEXT NOT NULL,
    column_type TEXT NOT NULL DEFAULT '',
    column_description TEXT NOT NULL DEFAULT '',
    business_terms TEXT[] NOT NULL DEFAULT '{}',
    metric_definition TEXT NOT NULL DEFAULT '',
    sample_values TEXT[] NOT NULL DEFAULT '{}',
    embedding vector(512),
    PRIMARY KEY (db_name, table_name, column_name)
);
"""

VALUE_INDEX_DDL = """
CREATE TABLE IF NOT EXISTS value_index (
    table_name TEXT NOT NULL,
    column_name TEXT NOT NULL,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS value_index_trgm ON value_index USING gin (value gin_trgm_ops);
"""


def _is_text_type(sqlite_type: str) -> bool:
    return sqlite_type.upper() == "TEXT"


def ensure_metadata_table(conn) -> None:
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    conn.execute(METADATA_DDL)
    conn.execute(VALUE_INDEX_DDL)
    conn.commit()


def seed_catalog(conn, tables, rows, *, db_name: str = "warehouse") -> None:
    """从表定义（含描述）+ 数据行，建实体表 + 灌元数据 + 建值索引。"""
    ensure_metadata_table(conn)
    for t in tables:
        cols = ", ".join(f'"{c.name}" {c.type}' for c in t.columns)
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{t.name}" ({cols})')
        conn.execute(f'DELETE FROM "{t.name}"')
        if t.name in rows:
            for r in rows[t.name]:
                ph = ", ".join(["%s"] * len(r))
                conn.execute(f'INSERT INTO "{t.name}" VALUES ({ph})', r)

    conn.execute("DELETE FROM schema_metadata")
    conn.execute("DELETE FROM value_index")
    for t in tables:
        table_rows = rows.get(t.name, [])
        col_index = {c.name: i for i, c in enumerate(t.columns)}
        for c in t.columns:
            idx = col_index[c.name]
            # 元数据
            sample = sorted({str(r[idx]) for r in table_rows if idx < len(r)})[:10]
            conn.execute(
                "INSERT INTO schema_metadata "
                "(db_name, table_name, table_description, column_name, column_type, column_description, sample_values) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (db_name, t.name, t.description, c.name, c.type, c.description, sample),
            )
            # 值索引（仅文本列）
            if _is_text_type(c.type):
                vals = {str(r[idx]) for r in table_rows if idx < len(r) and r[idx] is not None}
                for v in vals:
                    conn.execute(
                        "INSERT INTO value_index (table_name, column_name, value) VALUES (%s,%s,%s)",
                        (t.name, c.name, v),
                    )
    conn.commit()


def fetch_metadata(conn, db_name: str = "warehouse") -> list[dict]:
    cur = conn.execute(
        "SELECT table_name, table_description, column_name, column_type, column_description, "
        "business_terms, metric_definition, sample_values "
        "FROM schema_metadata WHERE db_name = %s ORDER BY table_name, column_name",
        (db_name,),
    )
    return [
        {
            "table_name": r[0],
            "table_description": r[1],
            "column_name": r[2],
            "column_type": r[3],
            "column_description": r[4],
            "business_terms": r[5],
            "metric_definition": r[6],
            "sample_values": r[7],
        }
        for r in cur.fetchall()
    ]


def search_values(conn, term: str, limit: int = 5) -> list[dict]:
    """在值索引里模糊匹配（pg_trgm，容错拼写），返回 (表,列,值,相似度)。"""
    cur = conn.execute(
        "SELECT table_name, column_name, value, similarity(value, %s) AS sim "
        "FROM value_index WHERE value %% %s ORDER BY sim DESC LIMIT %s",
        (term, term, limit),
    )
    return [
        {"table_name": r[0], "column_name": r[1], "value": r[2], "similarity": round(r[3], 3)}
        for r in cur.fetchall()
    ]


TABLE_EMBEDDING_DDL = """
CREATE TABLE IF NOT EXISTS table_embeddings (
    db_name TEXT NOT NULL,
    table_name TEXT NOT NULL,
    embedding vector(512),
    PRIMARY KEY (db_name, table_name)
);
"""


def _vec_to_str(vec) -> str:
    return "[" + ",".join(str(float(x)) for x in vec) + "]"


def seed_table_embeddings(conn, db_name: str, table_docs: list[tuple[str, str]], embed_fn) -> None:
    """离线预计算表文档的 embedding，存进 pgvector（查询时只嵌入问题、不做重复计算）。"""
    conn.execute(TABLE_EMBEDDING_DDL)
    for table_name, doc in table_docs:
        vec = _vec_to_str(embed_fn(doc))
        conn.execute(
            "INSERT INTO table_embeddings (db_name, table_name, embedding) VALUES (%s,%s,%s::vector) "
            "ON CONFLICT (db_name, table_name) DO UPDATE SET embedding = EXCLUDED.embedding",
            (db_name, table_name, vec),
        )
    conn.commit()


def search_embeddings(conn, db_name: str, q_vec, limit: int = 10) -> list[tuple[str, float]]:
    """按余弦相似度检索最近的表（q_vec 为问题向量）。"""
    q = _vec_to_str(q_vec)
    cur = conn.execute(
        "SELECT table_name, 1 - (embedding <=> %s::vector) AS sim "
        "FROM table_embeddings WHERE db_name = %s ORDER BY embedding <=> %s::vector ASC LIMIT %s",
        (q, db_name, q, limit),
    )
    return [(r[0], float(r[1])) for r in cur.fetchall()]
