"""SQL policy enforcement shared by local and MCP execution paths.

The MCP server is the trust boundary, so validation must happen there even when
an MCP client has already validated the same SQL.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError


_DENIED_EXPRESSIONS = (
    exp.Alter,
    exp.Command,
    exp.Copy,
    exp.Create,
    exp.Delete,
    exp.Drop,
    exp.Insert,
    exp.Merge,
    exp.Update,
)
_DENIED_FUNCTIONS = {
    "pg_sleep",
    "read_csv",
    "read_json",
    "read_parquet",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "lo_import",
    "lo_export",
    "dblink",
    "dblink_exec",
    "nextval",
    "set_config",
    "set_role",
    "pg_advisory_lock",
    "pg_advisory_xact_lock",
}
_ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)


@dataclass
class SQLPolicyResult:
    ok: bool
    code: str = "OK"
    message: str = ""
    normalized_sql: str = ""
    tables: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "normalized_sql": self.normalized_sql,
            "tables": self.tables,
        }


class SQLPolicy:
    """Parse and enforce a conservative read-only SQL policy."""

    def __init__(
        self,
        *,
        dialect: str = "sqlite",
        allowed_tables: set[str] | None = None,
        allowed_schemas: set[str] | None = None,
        allowed_catalogs: set[str] | None = None,
        max_sql_length: int = 20_000,
    ) -> None:
        self.dialect = dialect
        self.allowed_tables = (
            {t.lower() for t in allowed_tables}
            if allowed_tables is not None
            else None
        )
        self.allowed_schemas = (
            {s.lower() for s in allowed_schemas}
            if allowed_schemas is not None
            else None
        )
        self.allowed_catalogs = (
            {c.lower() for c in allowed_catalogs}
            if allowed_catalogs is not None
            else None
        )
        self.max_sql_length = max_sql_length

    def validate(self, sql: str) -> SQLPolicyResult:
        raw = (sql or "").strip()
        if not raw:
            return SQLPolicyResult(False, "EMPTY_SQL", "empty SQL statement")
        if len(raw) > self.max_sql_length:
            return SQLPolicyResult(False, "SQL_TOO_LONG", "SQL exceeds policy length limit")

        try:
            statements = sqlglot.parse(raw, read=self.dialect)
        except ParseError as exc:
            return SQLPolicyResult(False, "INVALID_SQL", f"SQL parse error: {exc}")
        if len(statements) != 1:
            return SQLPolicyResult(False, "MULTIPLE_STATEMENTS", "multiple statements are not allowed")

        statement = statements[0]
        if not isinstance(statement, _ALLOWED_ROOTS):
            return SQLPolicyResult(
                False,
                "POLICY_DENIED",
                f"only read queries are allowed, got {type(statement).__name__}",
            )
        if any(statement.find(node_type) for node_type in _DENIED_EXPRESSIONS):
            return SQLPolicyResult(False, "POLICY_DENIED", "write or DDL operation is not allowed")

        for fn in statement.find_all(exp.Func):
            name = (getattr(fn, "name", "") or fn.sql_name() or "").lower()
            if name in _DENIED_FUNCTIONS:
                return SQLPolicyResult(False, "POLICY_DENIED", f"function {name} is not allowed")

        cte_names = {
            cte.alias_or_name.lower()
            for cte in statement.find_all(exp.CTE)
            if cte.alias_or_name
        }
        table_nodes = [
            table
            for table in statement.find_all(exp.Table)
            if table.name and table.name.lower() not in cte_names
        ]
        if self.allowed_schemas is not None:
            denied_schemas = sorted(
                {
                    (table.db or "public").lower()
                    for table in table_nodes
                    if (table.db or "public").lower() not in self.allowed_schemas
                }
            )
            if denied_schemas:
                return SQLPolicyResult(
                    False,
                    "SCHEMA_NOT_ALLOWED",
                    "schema access is not allowed: " + ", ".join(denied_schemas),
                    tables=sorted({table.name.lower() for table in table_nodes}),
                )
        if self.allowed_catalogs is not None:
            denied_catalogs = sorted(
                {
                    (table.catalog or "").lower()
                    for table in table_nodes
                    if (table.catalog or "").lower() not in self.allowed_catalogs
                }
            )
            if denied_catalogs:
                return SQLPolicyResult(
                    False,
                    "DATABASE_NOT_ALLOWED",
                    "database qualification is not allowed: " + ", ".join(denied_catalogs),
                    tables=sorted({table.name.lower() for table in table_nodes}),
                )
        tables = sorted({table.name.lower() for table in table_nodes})
        if self.allowed_tables is not None:
            denied = [name for name in tables if name not in self.allowed_tables]
            if denied:
                return SQLPolicyResult(
                    False,
                    "TABLE_NOT_ALLOWED",
                    "table access is not allowed: " + ", ".join(denied),
                    tables=tables,
                )

        normalized = statement.sql(dialect=self.dialect)
        return SQLPolicyResult(True, normalized_sql=normalized, tables=tables)


def check_sql(sql: str, *, dialect: str = "sqlite", allowed_tables: set[str] | None = None) -> str | None:
    """Compatibility helper returning only a human-readable policy error."""
    result = SQLPolicy(dialect=dialect, allowed_tables=allowed_tables).validate(sql)
    return None if result.ok else result.message
