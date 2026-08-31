"""PostgreSQL MCP server for the QueryAgent Web Demo.

The server is intentionally a separate stdio process. It receives the DSN
from the backend process environment, never from the browser or an LLM prompt,
and delegates every query-time operation to ``PostgresDataService``.
"""
from __future__ import annotations

import os
import sys

from mcp.server.fastmcp import FastMCP

from .access import AccessConfig, AuditLog, load_access_config
from .postgres import (
    DEFAULT_MAX_ROWS,
    DEFAULT_STATEMENT_TIMEOUT_MS,
    PostgresDataService,
)


_SERVER_NAME = "queryagent-postgres-data"


def build_postgres_server(
    dsn: str,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
    service: PostgresDataService | None = None,
    access_config: AccessConfig | None = None,
    audit_log: AuditLog | None = None,
) -> FastMCP:
    """Build a PostgreSQL MCP server with all query-time policy enforcement."""
    service = service or PostgresDataService(
        dsn,
        max_rows=max_rows,
        statement_timeout_ms=statement_timeout_ms,
        access_config=access_config or load_access_config(),
        audit_log=audit_log or AuditLog(),
    )
    mcp = FastMCP(_SERVER_NAME)

    @mcp.tool()
    def get_schema(role: str = "", question: str = "", top_k: int | None = None) -> dict:
        """Return role-visible PostgreSQL DDL and sensitive-field metadata."""
        return service.get_schema(role=role, question=question, top_k=top_k)

    @mcp.tool()
    def search_values(term: str, role: str = "", limit: int = 5) -> dict:
        """Find real business values that may be used in a SQL filter."""
        return service.search_values(term=term, role=role, limit=limit)

    @mcp.tool()
    def validate_sql(sql: str, role: str = "") -> dict:
        """Validate one read-only SQL statement against role policy."""
        return service.validate_sql(sql=sql, role=role)

    @mcp.tool()
    def query(sql: str, role: str = "") -> dict:
        """Validate and execute one read-only SQL statement."""
        return service.query(sql=sql, role=role)

    @mcp.tool()
    def list_tables(role: str = "") -> dict:
        """List role-visible business tables and row counts."""
        return service.list_tables(role=role)

    @mcp.tool()
    def browse_table(
        table: str,
        role: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> dict:
        """Return one bounded page from a role-visible business table."""
        return service.browse_table(
            table=table,
            role=role,
            page=page,
            page_size=page_size,
        )

    @mcp.tool()
    def search_table(
        table: str,
        term: str,
        role: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> dict:
        """Search visible text columns in a business table and paginate."""
        return service.search_table(
            table=table,
            term=term,
            role=role,
            page=page,
            page_size=page_size,
        )

    @mcp.tool()
    def export_table_csv(
        table: str,
        role: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> dict:
        """Export one role-filtered table page as CSV."""
        return service.export_table_csv(
            table=table,
            role=role,
            page=page,
            page_size=page_size,
        )

    @mcp.tool()
    def export_query_csv(sql: str, role: str = "") -> dict:
        """Export one policy-approved query result as CSV."""
        return service.export_query_csv(sql=sql, role=role)

    return mcp


def main() -> None:
    # Keep the DSN out of process arguments. Only the backend's MCP child
    # environment supplies the database credential.
    dsn = os.environ.get("QUERYAGENT_MCP_DSN")
    if not dsn:
        raise SystemExit("QUERYAGENT_MCP_DSN is required")
    server = build_postgres_server(
        dsn,
        max_rows=int(os.environ.get("QUERYAGENT_MAX_ROWS", str(DEFAULT_MAX_ROWS))),
        statement_timeout_ms=int(
            os.environ.get(
                "QUERYAGENT_STATEMENT_TIMEOUT_MS",
                str(DEFAULT_STATEMENT_TIMEOUT_MS),
            )
        ),
    )
    server.run()


if __name__ == "__main__":
    main()
