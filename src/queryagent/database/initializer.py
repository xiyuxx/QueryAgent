"""Initialize and reset the PostgreSQL demo-production database.

This module is the only maintenance code allowed to connect directly to
PostgreSQL. Query-time access goes through the PostgreSQL MCP server. The
initializer is deterministic: resetting with the same seed produces the same
rows and embedding documents.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Protocol, Sequence

import psycopg
from psycopg import errors
from psycopg import sql as pgsql

from .production import (
    DEFAULT_DB_NAME,
    DEFAULT_SEED,
    PRODUCTION_TABLES,
    ProductionSnapshot,
    TableSpec,
    build_production_snapshot,
    iter_table_docs,
    quote_identifier,
    render_create_table,
)


DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_EMBEDDING_DIM = 512
DEFAULT_READER_ROLE = "queryagent_reader"
DEFAULT_READER_PASSWORD = "queryagent_reader"


class Embedder(Protocol):
    def embed(self, documents: Sequence[str]) -> Iterable[Sequence[float]]:
        """Return one vector for each input document."""


@dataclass(frozen=True)
class InitializationResult:
    seed: int
    snapshot_digest: str
    row_counts: dict[str, int]
    embedding_model: str
    embedding_dimension: int
    reader_user: str = DEFAULT_READER_ROLE

    @property
    def total_rows(self) -> int:
        return sum(self.row_counts.values())


class EmbeddingDimensionError(ValueError):
    """Raised when the configured embedding model does not match pgvector."""


def _load_default_embedder(model_name: str) -> Embedder:
    """Load fastembed lazily so schema-only tools do not download a model."""
    try:
        from fastembed import TextEmbedding
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "fastembed is required for database initialization; install the "
            "postgres extra or use the backend Docker image"
        ) from exc
    return TextEmbedding(model_name=model_name)


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in vector) + "]"


def _table_names_sql(tables: Iterable[TableSpec]) -> str:
    return ", ".join(quote_identifier(table.name) for table in tables)


def _create_internal_tables(conn: psycopg.Connection, embedding_dim: int) -> None:
    """Create metadata and vector tables used by retrieval."""
    if embedding_dim <= 0:
        raise ValueError("embedding_dim must be positive")
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS "schema_metadata" (
            "db_name" TEXT NOT NULL DEFAULT 'queryagent_demo',
            "table_name" TEXT NOT NULL,
            "table_description" TEXT NOT NULL DEFAULT '',
            "column_name" TEXT NOT NULL,
            "column_type" TEXT NOT NULL DEFAULT '',
            "column_description" TEXT NOT NULL DEFAULT '',
            "business_terms" TEXT[] NOT NULL DEFAULT '{{}}',
            "metric_definition" TEXT NOT NULL DEFAULT '',
            "sample_values" TEXT[] NOT NULL DEFAULT '{{}}',
            "sensitive" BOOLEAN NOT NULL DEFAULT FALSE,
            "embedding" vector({embedding_dim}),
            PRIMARY KEY ("db_name", "table_name", "column_name")
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS "value_index" (
            "table_name" TEXT NOT NULL,
            "column_name" TEXT NOT NULL,
            "value" TEXT NOT NULL,
            "sensitive" BOOLEAN NOT NULL DEFAULT FALSE,
            PRIMARY KEY ("table_name", "column_name", "value")
        )
        """
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS "value_index_trgm" '
        'ON "value_index" USING gin ("value" gin_trgm_ops)'
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS "table_embeddings" (
            "db_name" TEXT NOT NULL,
            "table_name" TEXT NOT NULL,
            "embedding" vector({embedding_dim}) NOT NULL,
            PRIMARY KEY ("db_name", "table_name")
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS "queryagent_state" (
            "key" TEXT PRIMARY KEY,
            "value" TEXT NOT NULL,
            "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def _create_business_tables(conn: psycopg.Connection, tables: Sequence[TableSpec]) -> None:
    for table in tables:
        conn.execute(render_create_table(table))


def _configure_reader_role(
    conn: psycopg.Connection,
    tables: Sequence[TableSpec],
    *,
    user: str = DEFAULT_READER_ROLE,
    password: str,
) -> None:
    """Create/update the database-level read-only boundary used by MCP."""
    role = pgsql.Identifier(user)
    password_value = pgsql.Literal(password)
    role_exists = conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s",
        (user,),
    ).fetchone()
    if role_exists:
        conn.execute(
            pgsql.SQL(
                "ALTER ROLE {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS LOGIN PASSWORD {}"
            ).format(role, password_value)
        )
    else:
        conn.execute(
            pgsql.SQL(
                "CREATE ROLE {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS LOGIN PASSWORD {}"
            ).format(role, password_value)
        )

    # Revoke privileges from this reader role only. The demo database's admin
    # owner keeps its maintenance privileges, while MCP remains read-only at
    # both the database-role and application-policy layers.
    all_tables = [
        *(table.name for table in tables),
        "schema_metadata",
        "value_index",
        "table_embeddings",
        "queryagent_state",
    ]
    # PUBLIC may otherwise retain CREATE on the default public schema, which
    # would undermine the reader role through inherited privileges.
    conn.execute('REVOKE CREATE ON SCHEMA "public" FROM PUBLIC')
    conn.execute(
        pgsql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(
            pgsql.Identifier("public"), role
        )
    )
    conn.execute(
        pgsql.SQL("REVOKE ALL ON TABLE {} FROM {}").format(
            pgsql.SQL(", ").join(pgsql.Identifier(name) for name in all_tables),
            role,
        )
    )
    conn.execute(pgsql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
        pgsql.Identifier("public"), role
    ))
    conn.execute(
        pgsql.SQL("GRANT SELECT ON TABLE {} TO {}").format(
            pgsql.SQL(", ").join(pgsql.Identifier(name) for name in all_tables),
            role,
        )
    )
    conn.execute(
        pgsql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO {}").format(
            pgsql.Identifier("public"), role
        )
    )


def _clear_business_data(conn: psycopg.Connection, tables: Sequence[TableSpec]) -> None:
    # CASCADE makes the reset independent of the dependency order and keeps
    # the operation atomic inside the caller's transaction.
    conn.execute(f"TRUNCATE TABLE {_table_names_sql(tables)} RESTART IDENTITY CASCADE")


def _insert_business_rows(
    conn: psycopg.Connection,
    snapshot: ProductionSnapshot,
) -> None:
    for table in snapshot.tables:
        rows = snapshot.rows.get(table.name, [])
        if not rows:
            continue
        columns = ", ".join(quote_identifier(column) for column in table.column_names)
        placeholders = ", ".join(["%s"] * len(table.columns))
        statement = f"INSERT INTO {quote_identifier(table.name)} ({columns}) VALUES ({placeholders})"
        with conn.cursor() as cursor:
            cursor.executemany(statement, rows)


def _clear_retrieval_indexes(conn: psycopg.Connection) -> None:
    conn.execute('TRUNCATE TABLE "schema_metadata", "value_index", "table_embeddings"')


def _insert_metadata(conn: psycopg.Connection, snapshot: ProductionSnapshot) -> None:
    for table in snapshot.tables:
        table_rows = snapshot.rows.get(table.name, [])
        for column_index, column in enumerate(table.columns):
            sample_values = sorted(
                {
                    str(row[column_index])
                    for row in table_rows
                    if column_index < len(row) and row[column_index] is not None
                }
            )[:10]
            conn.execute(
                """
                INSERT INTO "schema_metadata"
                    ("db_name", "table_name", "table_description", "column_name",
                     "column_type", "column_description", "sample_values", "sensitive")
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    DEFAULT_DB_NAME,
                    table.name,
                    table.description,
                    column.name,
                    column.pg_type,
                    column.description,
                    sample_values,
                    column.sensitive,
                ),
            )
            if column.pg_type.upper().startswith(("TEXT", "VARCHAR", "CHAR")):
                values = sorted(
                    {
                        str(row[column_index])
                        for row in table_rows
                        if column_index < len(row) and row[column_index] is not None
                    }
                )
                for value in values:
                    conn.execute(
                        """
                        INSERT INTO "value_index" ("table_name", "column_name", "value", "sensitive")
                        VALUES (%s, %s, %s, %s)
                        """,
                        (table.name, column.name, value, column.sensitive),
                    )


def _insert_table_embeddings(
    conn: psycopg.Connection,
    tables: Sequence[TableSpec],
    embedder: Embedder,
    embedding_model: str,
    embedding_dim: int,
) -> None:
    docs = [doc for _, doc in iter_table_docs(tables)]
    vectors = list(embedder.embed(docs))
    if len(vectors) != len(docs):
        raise EmbeddingDimensionError(
            f"embedder returned {len(vectors)} vectors for {len(docs)} documents"
        )
    for table, vector in zip(tables, vectors):
        values = list(vector)
        if len(values) != embedding_dim:
            raise EmbeddingDimensionError(
                f"embedding model {embedding_model!r} returned dimension {len(values)}; "
                f"expected {embedding_dim}"
            )
        conn.execute(
            """
            INSERT INTO "table_embeddings" ("db_name", "table_name", "embedding")
            VALUES (%s, %s, %s::vector)
            ON CONFLICT ("db_name", "table_name")
            DO UPDATE SET "embedding" = EXCLUDED."embedding"
            """,
            (DEFAULT_DB_NAME, table.name, _vector_literal(values)),
        )


def _write_state(conn: psycopg.Connection, result: InitializationResult) -> None:
    state = {
        "seed": str(result.seed),
        "snapshot_digest": result.snapshot_digest,
        "total_rows": str(result.total_rows),
        "embedding_model": result.embedding_model,
        "embedding_dimension": str(result.embedding_dimension),
        "reader_role": result.reader_user,
    }
    for key, value in state.items():
        conn.execute(
            """
            INSERT INTO "queryagent_state" ("key", "value", "updated_at")
            VALUES (%s, %s, NOW())
            ON CONFLICT ("key") DO UPDATE
            SET "value" = EXCLUDED."value", "updated_at" = EXCLUDED."updated_at"
            """,
            (key, value),
        )


def _initialize_on_connection(
    conn: psycopg.Connection,
    *,
    snapshot: ProductionSnapshot,
    embedder: Embedder,
    embedding_model: str,
    embedding_dim: int,
    reader_user: str,
    reader_password: str,
    reset: bool,
) -> InitializationResult:
    _create_internal_tables(conn, embedding_dim)
    _create_business_tables(conn, snapshot.tables)
    _configure_reader_role(
        conn,
        snapshot.tables,
        user=reader_user,
        password=reader_password,
    )
    if reset:
        _clear_business_data(conn, snapshot.tables)
    _insert_business_rows(conn, snapshot)
    _clear_retrieval_indexes(conn)
    _insert_metadata(conn, snapshot)
    _insert_table_embeddings(conn, snapshot.tables, embedder, embedding_model, embedding_dim)
    result = InitializationResult(
        seed=snapshot.seed,
        snapshot_digest=snapshot.digest,
        row_counts=snapshot.row_counts,
        embedding_model=embedding_model,
        embedding_dimension=embedding_dim,
        reader_user=reader_user,
    )
    _write_state(conn, result)
    return result


def initialize_production_database(
    dsn: str,
    *,
    seed: int = DEFAULT_SEED,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    embedding_dim: int = DEFAULT_EMBEDDING_DIM,
    embedder: Embedder | None = None,
    reader_user: str | None = None,
    reader_password: str | None = None,
) -> InitializationResult:
    """Create or refresh the demo database using deterministic data.

    ``embedder`` is injectable for tests. When omitted, constructing the
    default ``TextEmbedding`` downloads the model on first use and then relies
    on fastembed's local cache.
    """
    snapshot = build_production_snapshot(seed)
    actual_embedder = embedder or _load_default_embedder(embedding_model)
    actual_reader_user = reader_user or os.environ.get(
        "QUERYAGENT_READER_USER", DEFAULT_READER_ROLE
    )
    actual_reader_password = reader_password or os.environ.get(
        "QUERYAGENT_READER_PASSWORD", DEFAULT_READER_PASSWORD
    )
    with psycopg.connect(dsn) as conn:
        result = _initialize_on_connection(
            conn,
            snapshot=snapshot,
            embedder=actual_embedder,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
            reader_user=actual_reader_user,
            reader_password=actual_reader_password,
            reset=True,
        )
        conn.commit()
        return result


def ensure_production_database(
    dsn: str,
    *,
    seed: int = DEFAULT_SEED,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    embedding_dim: int = DEFAULT_EMBEDDING_DIM,
    embedder: Embedder | None = None,
    reader_user: str | None = None,
    reader_password: str | None = None,
) -> InitializationResult:
    """Initialize only when the database is absent or from another snapshot.

    Compose calls this on every start. A matching state row avoids rebuilding
    rows and, importantly, avoids downloading/loading the embedding model on
    ordinary restarts. A deliberate reset must call
    ``reset_production_database`` instead.
    """
    snapshot = build_production_snapshot(seed)
    actual_reader_user = reader_user or os.environ.get(
        "QUERYAGENT_READER_USER", DEFAULT_READER_ROLE
    )
    actual_reader_password = reader_password or os.environ.get(
        "QUERYAGENT_READER_PASSWORD", DEFAULT_READER_PASSWORD
    )
    with psycopg.connect(dsn) as conn:
        try:
            state_rows = conn.execute(
                'SELECT "key", "value" FROM "queryagent_state" '
                'WHERE "key" IN (%s, %s, %s, %s, %s, %s)',
                (
                    "seed",
                    "snapshot_digest",
                    "embedding_model",
                    "embedding_dimension",
                    "total_rows",
                    "reader_role",
                ),
            ).fetchall()
        except errors.UndefinedTable:
            conn.rollback()
            state_rows = []
        state = dict(state_rows)
        matches = state == {
            "seed": str(seed),
            "snapshot_digest": snapshot.digest,
            "embedding_model": embedding_model,
            "embedding_dimension": str(embedding_dim),
            "total_rows": str(snapshot.total_rows),
            "reader_role": actual_reader_user,
        }
        if matches:
            _configure_reader_role(
                conn,
                PRODUCTION_TABLES,
                user=actual_reader_user,
                password=actual_reader_password,
            )
            conn.commit()
            return InitializationResult(
                seed=seed,
                snapshot_digest=snapshot.digest,
                row_counts=snapshot.row_counts,
                embedding_model=embedding_model,
                embedding_dimension=embedding_dim,
                reader_user=actual_reader_user,
            )

    return initialize_production_database(
        dsn,
        seed=seed,
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        embedder=embedder,
        reader_user=actual_reader_user,
        reader_password=actual_reader_password,
    )


def reset_production_database(
    dsn: str,
    *,
    seed: int = DEFAULT_SEED,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    embedding_dim: int = DEFAULT_EMBEDDING_DIM,
    embedder: Embedder | None = None,
    reader_user: str | None = None,
    reader_password: str | None = None,
) -> InitializationResult:
    """Restore exactly the same fixed snapshot as initialization."""
    return initialize_production_database(
        dsn,
        seed=seed,
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        embedder=embedder,
        reader_user=reader_user,
        reader_password=reader_password,
    )


def initialization_from_environment(dsn: str | None = None, **kwargs) -> InitializationResult:
    """Convenience entry point for the Compose init command."""
    resolved_dsn = dsn or os.environ["QUERYAGENT_DB_DSN"]
    options = {
        "embedding_model": os.environ.get(
            "QUERYAGENT_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL
        ),
        "embedding_dim": int(
            os.environ.get("QUERYAGENT_EMBEDDING_DIM", str(DEFAULT_EMBEDDING_DIM))
        ),
        "reader_user": os.environ.get(
            "QUERYAGENT_READER_USER", DEFAULT_READER_ROLE
        ),
        "reader_password": os.environ.get(
            "QUERYAGENT_READER_PASSWORD", DEFAULT_READER_PASSWORD
        ),
        **kwargs,
    }
    return initialize_production_database(resolved_dsn, **options)
