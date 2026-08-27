"""MCP data server: the policy boundary between an agent and a database.

The model never receives a DSN or filesystem path. The server validates every
SQL statement and enforces role-based access before delegating to the sandbox.

Usage:
    python -m queryagent.tools.mcp_server <db_path> [subprocess|docker]

Optional environment variables:
    QUERYAGENT_ROLES_CONFIG=path/to/queryagent_roles.yaml
    QUERYAGENT_AUDIT_LOG=path/to/audit.jsonl
    QUERYAGENT_MAX_ROWS=100
"""
from __future__ import annotations

import os
import sqlite3
import sys
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .access import AccessConfig, AuditLog, load_access_config
from .policy import SQLPolicy
from .sandbox import SandboxExecutor


def _schema_context(
    db_path: str,
    allowed_tables: set[str] | None,
    blocked_columns: dict[str, set[str]],
) -> dict:
    """Return DDL filtered by role: omit disallowed tables and blocked columns."""
    conn = sqlite3.connect(db_path)
    try:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        if allowed_tables is not None:
            names = [n for n in names if n.lower() in allowed_tables]

        ddl: list[str] = []
        for name in names:
            cols = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
            blocked = blocked_columns.get(name.lower(), set())
            visible = [c for c in cols if c[1].lower() not in blocked]
            col_defs = ", ".join(f"{c[1]} {c[2]}" for c in visible)
            ddl.append(f"CREATE TABLE {name} ({col_defs});")
        return {"ddl": "\n".join(ddl), "tables": names}
    finally:
        conn.close()


def _strip_blocked_cols(
    columns: list[str],
    rows: list[list],
    table_names: list[str],
    blocked_columns: dict[str, set[str]],
) -> tuple[list[str], list[list]]:
    """Remove blocked columns from a result set (post-execution guard)."""
    # Collect every blocked column across all tables touched by this query
    all_blocked = set()
    for t in table_names:
        all_blocked.update(blocked_columns.get(t.lower(), set()))
    if not all_blocked:
        return columns, rows
    keep = [i for i, c in enumerate(columns) if c.lower() not in all_blocked]
    filtered_cols = [columns[i] for i in keep]
    filtered_rows = [[row[i] for i in keep] for row in rows]
    return filtered_cols, filtered_rows


def build_server(
    db_path: str,
    backend: str = "subprocess",
    *,
    max_rows: int = 100,
    access_config: AccessConfig | None = None,
    audit_log: AuditLog | None = None,
) -> FastMCP:
    """Build a SQLite MCP server with role-based access and audit logging."""
    resolved_path = str(Path(db_path).resolve())
    cfg = access_config or load_access_config()
    log = audit_log or AuditLog()
    policy = SQLPolicy(dialect="sqlite")   # table allowlist is role-driven, not global
    executor = SandboxExecutor(resolved_path, backend=backend, max_rows=max_rows)
    mcp = FastMCP("queryagent-data")

    @mcp.tool()
    def get_schema(role: str = "") -> dict:
        """Return DDL for tables the caller's role is allowed to see."""
        role_policy = cfg.get_role(role or None)
        if role_policy is None:
            log.denied(role, "get_schema", [], "ACCESS_DENIED", f"unknown role: {role!r}")
            return {"ddl": "", "tables": [], "error": {"code": "ACCESS_DENIED", "message": f"unknown role: {role!r}"}}
        result = _schema_context(resolved_path, role_policy.allowed_tables, role_policy.blocked_columns)
        log.allowed(role_policy.name, "get_schema", result["tables"])
        return result

    @mcp.tool()
    def validate_sql(sql: str, role: str = "") -> dict:
        """Validate a read-only SQL statement against the caller's role policy."""
        role_policy = cfg.get_role(role or None)
        if role_policy is None:
            log.denied(role, "validate_sql", [], "ACCESS_DENIED", f"unknown role: {role!r}")
            return {"ok": False, "code": "ACCESS_DENIED", "message": f"unknown role: {role!r}",
                    "normalized_sql": "", "tables": []}
        checked = policy.validate(sql)
        if not checked.ok:
            log.denied(role_policy.name, "validate_sql", checked.tables, checked.code, checked.message)
            return checked.to_dict()
        denied_tables = [t for t in checked.tables if not role_policy.may_access_table(t)]
        if denied_tables:
            msg = "role does not have access to: " + ", ".join(denied_tables)
            log.denied(role_policy.name, "validate_sql", checked.tables, "TABLE_NOT_ALLOWED", msg)
            return {"ok": False, "code": "TABLE_NOT_ALLOWED", "message": msg,
                    "normalized_sql": "", "tables": checked.tables}
        log.allowed(role_policy.name, "validate_sql", checked.tables)
        return checked.to_dict()

    @mcp.tool()
    def query(sql: str, role: str = "") -> dict:
        """Execute a policy-approved read query within the caller's role permissions."""
        query_id = uuid.uuid4().hex
        role_policy = cfg.get_role(role or None)
        if role_policy is None:
            log.denied(role, "query", [], "ACCESS_DENIED", f"unknown role: {role!r}", query_id)
            return {"columns": [], "rows": [], "truncated": False, "query_id": query_id,
                    "tables": [], "error": {"code": "ACCESS_DENIED", "message": f"unknown role: {role!r}", "retryable": False}}

        # SQL safety check
        checked = policy.validate(sql)
        if not checked.ok:
            log.denied(role_policy.name, "query", checked.tables, checked.code, checked.message, query_id)
            return {"columns": [], "rows": [], "truncated": False, "query_id": query_id,
                    "tables": checked.tables,
                    "error": {"code": checked.code, "message": checked.message, "retryable": False}}

        # Role table access check
        denied_tables = [t for t in checked.tables if not role_policy.may_access_table(t)]
        if denied_tables:
            msg = "role does not have access to: " + ", ".join(denied_tables)
            log.denied(role_policy.name, "query", checked.tables, "TABLE_NOT_ALLOWED", msg, query_id)
            return {"columns": [], "rows": [], "truncated": False, "query_id": query_id,
                    "tables": checked.tables,
                    "error": {"code": "TABLE_NOT_ALLOWED", "message": msg, "retryable": False}}

        # Execute
        result = executor.execute(checked.normalized_sql)
        if result.error is not None:
            log.denied(role_policy.name, "query", checked.tables, "DATABASE_ERROR", result.error, query_id)
            return {"columns": [], "rows": [], "truncated": False, "query_id": query_id,
                    "tables": checked.tables,
                    "error": {"code": "DATABASE_ERROR", "message": result.error, "retryable": False}}

        # Strip blocked columns from results
        cols, rows = _strip_blocked_cols(
            result.columns, [list(r) for r in result.rows],
            checked.tables, role_policy.blocked_columns,
        )
        log.allowed(role_policy.name, "query", checked.tables, query_id)
        return {
            "columns": cols,
            "rows": rows,
            "truncated": result.truncated,
            "query_id": query_id,
            "tables": checked.tables,
            "error": None,
        }

    return mcp


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "data/sales.db"
    backend = sys.argv[2] if len(sys.argv) > 2 else "subprocess"
    max_rows = int(os.environ.get("QUERYAGENT_MAX_ROWS", "100"))
    build_server(db_path, backend, max_rows=max_rows).run()


if __name__ == "__main__":
    main()
