from __future__ import annotations

from dataclasses import dataclass

import pytest

from queryagent.database.initializer import (
    EmbeddingDimensionError,
    InitializationResult,
    _insert_table_embeddings,
    _vector_literal,
)
from queryagent.database.production import PRODUCTION_TABLES


@dataclass
class FakeCursor:
    statements: list[tuple[str, tuple]]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def executemany(self, statement, rows):
        self.statements.append((statement, tuple(rows)))


class FakeConnection:
    def __init__(self):
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append((statement, params))

    def cursor(self):
        return FakeCursor(self.statements)


class FakeEmbedder:
    def __init__(self, dimension: int):
        self.dimension = dimension

    def embed(self, documents):
        return [[float(index) for index in range(self.dimension)] for _ in documents]


def test_vector_literal_is_pgvector_compatible() -> None:
    assert _vector_literal([0, 0.25, 1]) == "[0.0,0.25,1.0]"


def test_insert_table_embeddings_writes_one_vector_per_table() -> None:
    conn = FakeConnection()
    _insert_table_embeddings(
        conn,
        PRODUCTION_TABLES,
        FakeEmbedder(4),
        "test-embedder",
        4,
    )

    inserts = [statement for statement, _params in conn.statements if "table_embeddings" in statement]
    assert len(inserts) == len(PRODUCTION_TABLES)
    assert all("::vector" in statement for statement in inserts)


def test_insert_table_embeddings_rejects_wrong_dimension() -> None:
    with pytest.raises(EmbeddingDimensionError, match="expected 4"):
        _insert_table_embeddings(
            FakeConnection(),
            PRODUCTION_TABLES,
            FakeEmbedder(3),
            "test-embedder",
            4,
        )


def test_initialization_result_reports_total_rows() -> None:
    result = InitializationResult(
        seed=1,
        snapshot_digest="digest",
        row_counts={"customers": 2, "orders": 3},
        embedding_model="test",
        embedding_dimension=4,
    )
    assert result.total_rows == 5
