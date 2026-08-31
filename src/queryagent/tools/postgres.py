"""PostgreSQL data service used by the PostgreSQL MCP server.

All query-time data access in the Web Demo goes through this service. It owns
role checks, SQL policy checks, sensitive-column rules, pagination, search and
result shaping. The FastAPI process never imports this service directly for
normal requests; it talks to the service through ``MCPExecutor``/stdio.
"""
from __future__ import annotations

import csv
import io
import math
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Callable, Iterable, Iterator, Sequence

import sqlglot
from sqlglot import exp

from ..database.production import (
    BUSINESS_TABLE_NAMES,
    INTERNAL_TABLE_NAMES,
    PRODUCTION_TABLES,
)
from .access import AccessConfig, AuditLog, RolePolicy, load_access_config
from .db import QueryResult
from .policy import SQLPolicy, SQLPolicyResult


_PUBLIC_SCHEMA = "public"

try:  # psycopg is an optional dependency outside the PostgreSQL Docker image.
    import psycopg
except ImportError:  # pragma: no cover - exercised only without postgres extra
    psycopg = None  # type: ignore[assignment]


DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
DEFAULT_MAX_ROWS = 100
DEFAULT_STATEMENT_TIMEOUT_MS = 5_000

_KNOWN_SENSITIVE_COLUMNS = {
    table.name.lower(): {column.name.lower() for column in table.columns if column.sensitive}
    for table in PRODUCTION_TABLES
}


@dataclass(frozen=True)
class ColumnDescriptor:
    name: str
    data_type: str
    nullable: bool = True
    ordinal_position: int = 0
    sensitive: bool = False


@dataclass(frozen=True)
class TableDescriptor:
    name: str
    columns: tuple[ColumnDescriptor, ...]
    row_count: int | None = None

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    @property
    def sensitive_columns(self) -> frozenset[str]:
        return frozenset(column.name.lower() for column in self.columns if column.sensitive)


@dataclass(frozen=True)
class SensitiveReference:
    table: str
    column: str


@dataclass
class _Catalog:
    tables: dict[str, TableDescriptor] = field(default_factory=dict)

    @property
    def business_tables(self) -> set[str]:
        return set(self.tables)


def _json_value(value: Any) -> Any:
    """Convert common psycopg values to MCP/JSON-safe values."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _json_rows(rows: Iterable[Sequence[Any]]) -> list[list[Any]]:
    return [[_json_value(value) for value in row] for row in rows]


def mask_sensitive_rows(
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    sensitive_columns: set[str] | frozenset[str],
    raw_columns: set[str] | frozenset[str] = frozenset(),
    mask: str = "******",
) -> tuple[list[str], list[list[Any]]]:
    """Mask sensitive result positions without removing their field names."""
    sensitive = {column.lower() for column in sensitive_columns}
    raw = {column.lower() for column in raw_columns}
    mask_indexes = {
        index
        for index, column in enumerate(columns)
        if column.lower() in sensitive and column.lower() not in raw
    }
    shaped: list[list[Any]] = []
    for row in rows:
        shaped.append([
            mask if index in mask_indexes else _json_value(value)
            for index, value in enumerate(row)
        ])
    return list(columns), shaped


def _table_aliases(statement: exp.Expression, catalog: _Catalog) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for table in statement.find_all(exp.Table):
        name = table.name.lower()
        if name not in catalog.tables:
            continue
        aliases[name] = name
        alias = (table.alias_or_name or name).lower()
        aliases[alias] = name
    return aliases


def find_sensitive_references(
    sql: str,
    catalog: _Catalog | dict[str, TableDescriptor],
    referenced_tables: Sequence[str] | None = None,
) -> list[SensitiveReference]:
    """Find explicit sensitive-column references in a parsed SQL statement.

    ``SELECT *`` is intentionally not considered explicit: it is allowed and
    the result shaper masks sensitive positions. A named sensitive column in a
    projection, predicate, ordering, grouping or CTE is rejected for roles
    without raw access, preventing a model from using a hidden value in a
    WHERE clause to infer information.
    """
    catalog_obj = catalog if isinstance(catalog, _Catalog) else _Catalog(dict(catalog))
    try:
        statement = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return []
    aliases = _table_aliases(statement, catalog_obj)
    tables = [
        table.lower()
        for table in (referenced_tables or aliases.values())
        if table.lower() in catalog_obj.tables
    ]
    candidates: set[tuple[str, str]] = set()
    # PostgreSQL can expose a whole row as a composite value (``SELECT c``),
    # or serialize it with row_to_json/to_jsonb. Treat those forms as an
    # explicit request for every sensitive field in that table.
    composite_qualifiers: set[str] = set()
    for column in statement.find_all(exp.Column):
        column_name = (column.name or "").lower()
        column_table = (column.table or "").lower()
        if column_table and column_name == column_table:
            composite_qualifiers.add(column_table)
        if not column_table and column_name in aliases:
            composite_qualifiers.add(column_name)
    for expression in statement.find_all(exp.Anonymous):
        if expression.name.lower() not in {"row_to_json", "to_json", "to_jsonb", "json_build_object"}:
            continue
        for column in expression.find_all(exp.Column):
            if column.table:
                composite_qualifiers.add(column.table.lower())
            elif column.name.lower() in aliases:
                composite_qualifiers.add(column.name.lower())
    # Parenthesized row expansion, e.g. ``(customers).*``, is represented by
    # sqlglot as an unqualified column whose parent is ``Paren``.
    for column in statement.find_all(exp.Column):
        if isinstance(column.parent, exp.Paren) and not column.table and column.name.lower() in aliases:
            composite_qualifiers.add(column.name.lower())
    for qualifier in composite_qualifiers:
        resolved = aliases.get(qualifier, qualifier)
        descriptor = catalog_obj.tables.get(resolved)
        if descriptor:
            candidates.update((resolved, name) for name in descriptor.sensitive_columns)

    for column in statement.find_all(exp.Column):
        column_name = (column.name or "").lower()
        if not column_name or column_name == "*":
            continue
        qualifier = (column.table or "").lower()
        if qualifier:
            resolved = aliases.get(qualifier, qualifier)
            descriptor = catalog_obj.tables.get(resolved)
            if descriptor and column_name in descriptor.sensitive_columns:
                candidates.add((resolved, column_name))
            continue
        for table_name in tables:
            descriptor = catalog_obj.tables[table_name]
            if column_name in descriptor.sensitive_columns:
                candidates.add((table_name, column_name))
    return [SensitiveReference(table, column) for table, column in sorted(candidates)]


def _error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    tables: Sequence[str] = (),
    query_id: str | None = None,
) -> dict[str, Any]:
    return {
        "columns": [],
        "rows": [],
        "truncated": False,
        "query_id": query_id,
        "tables": list(tables),
        "error": {"code": code, "message": message, "retryable": retryable},
    }


class PostgresDataService:
    """Role-aware PostgreSQL operations registered as MCP tools."""

    def __init__(
        self,
        dsn: str,
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
        statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
        access_config: AccessConfig | None = None,
        audit_log: AuditLog | None = None,
        connection_factory: Callable[..., Any] | None = None,
        catalog_loader: Callable[[Any], _Catalog] | None = None,
    ) -> None:
        if max_rows <= 0:
            raise ValueError("max_rows must be positive")
        if statement_timeout_ms <= 0:
            raise ValueError("statement_timeout_ms must be positive")
        self.dsn = dsn
        self.max_rows = max_rows
        self.statement_timeout_ms = statement_timeout_ms
        self.access_config = access_config or load_access_config()
        self.audit_log = audit_log or AuditLog()
        self._connection_factory = connection_factory
        self._catalog_loader = catalog_loader
        self._catalog_cache: _Catalog | None = None
        self.policy = SQLPolicy(
            dialect="postgres",
            allowed_tables=set(BUSINESS_TABLE_NAMES),
            allowed_schemas={_PUBLIC_SCHEMA},
            # Empty catalog means the current database; qualified cross-database
            # references are never part of the MCP contract.
            allowed_catalogs={""},
        )

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory(self.dsn)
        if psycopg is None:  # pragma: no cover - optional dependency guard
            raise RuntimeError("psycopg is required for the PostgreSQL MCP server")
        return psycopg.connect(self.dsn, autocommit=True)

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        conn = self._connect()
        try:
            conn.execute("SET default_transaction_read_only = on")
            conn.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (f"{self.statement_timeout_ms}ms",),
            )
            yield conn
        finally:
            close = getattr(conn, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _load_catalog_from_connection(conn: Any) -> _Catalog:
        column_rows = conn.execute(
            """
            SELECT table_name, column_name, data_type, is_nullable, ordinal_position
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name <> ALL(%s)
            ORDER BY table_name, ordinal_position
            """,
            (_PUBLIC_SCHEMA, list(INTERNAL_TABLE_NAMES)),

        ).fetchall()
        sensitive: dict[tuple[str, str], bool] = {
            (table_name, column_name): True
            for table_name, columns in _KNOWN_SENSITIVE_COLUMNS.items()
            for column_name in columns
        }
        try:
            metadata_rows = conn.execute(
                """
                SELECT table_name, column_name, sensitive
                FROM schema_metadata
                WHERE db_name = %s
                """,
                ("queryagent_demo",),
            ).fetchall()
            sensitive.update(
                {
                    (str(table).lower(), str(column).lower()): bool(flag)
                    for table, column, flag in metadata_rows
                    if bool(flag)
                }
            )
        except Exception:
            # Keep the static production-schema sensitive map if metadata is
            # temporarily unavailable; security must fail closed.
            rollback = getattr(conn, "rollback", None)
            if callable(rollback):
                rollback()
        tables: dict[str, list[ColumnDescriptor]] = {}
        for table, column, data_type, nullable, ordinal in column_rows:
            table_name = str(table).lower()
            column_name = str(column)
            tables.setdefault(table_name, []).append(
                ColumnDescriptor(
                    name=column_name,
                    data_type=str(data_type),
                    nullable=str(nullable).upper() == "YES",
                    ordinal_position=int(ordinal),
                    sensitive=sensitive.get((table_name, column_name.lower()), False),
                )
            )
        return _Catalog({
            table: TableDescriptor(name=table, columns=tuple(columns))
            for table, columns in tables.items()
            if table in BUSINESS_TABLE_NAMES
        })

    def _catalog(self, conn: Any, *, refresh: bool = False) -> _Catalog:
        if self._catalog_cache is None or refresh:
            self._catalog_cache = (
                self._catalog_loader(conn)
                if self._catalog_loader is not None
                else self._load_catalog_from_connection(conn)
            )
        return self._catalog_cache

    def refresh_catalog(self) -> None:
        self._catalog_cache = None

    def _role(self, role: str | None) -> RolePolicy | None:
        return self.access_config.get_role(role or None)

    @staticmethod
    def _public_identifier(name: str) -> str:
        """Quote an identifier that has already passed a catalog allowlist."""
        return '"' + name.replace('"', '""') + '"'

    @staticmethod
    def _role_tables(role_policy: RolePolicy, catalog: _Catalog) -> list[str]:
        return sorted(
            table_name
            for table_name in catalog.tables
            if role_policy.may_access_table(table_name)
        )

    def _validate(
        self,
        sql: str,
        role: str,
        catalog: _Catalog,
    ) -> tuple[RolePolicy | None, SQLPolicyResult, list[SensitiveReference]]:
        role_policy = self._role(role)
        if role_policy is None:
            return None, SQLPolicyResult(False, "ACCESS_DENIED", f"unknown role: {role!r}"), []
        checked = self.policy.validate(sql)
        if not checked.ok:
            return role_policy, checked, []
        denied_tables = [
            table for table in checked.tables if not role_policy.may_access_table(table)
        ]
        if denied_tables:
            return role_policy, SQLPolicyResult(
                False,
                "TABLE_NOT_ALLOWED",
                "role does not have access to: " + ", ".join(denied_tables),
                tables=checked.tables,
            ), []
        refs = find_sensitive_references(sql, catalog, checked.tables)
        denied_sensitive = [
            ref for ref in refs if not role_policy.may_return_raw(ref.table, ref.column)
        ]
        if denied_sensitive:
            detail = ", ".join(f"{ref.table}.{ref.column}" for ref in denied_sensitive)
            return role_policy, SQLPolicyResult(
                False,
                "SENSITIVE_COLUMN",
                "explicit access to sensitive column is not allowed: " + detail,
                tables=checked.tables,
            ), refs
        return role_policy, checked, refs

    def get_schema(self, *, role: str = "", question: str = "", top_k: int | None = None) -> dict[str, Any]:
        del question  # semantic ranking is added when the MCP retriever is wired in Phase 3
        with self._connection() as conn:
            catalog = self._catalog(conn)
            role_policy = self._role(role)
            if role_policy is None:
                message = f"unknown role: {role!r}"
                self.audit_log.denied(role, "get_schema", [], "ACCESS_DENIED", message)
                return {"ddl": "", "tables": [], "error": {"code": "ACCESS_DENIED", "message": message}}
            tables = self._role_tables(role_policy, catalog)
            if top_k is not None:
                tables = tables[: max(0, top_k)]
            ddl_parts: list[str] = []
            sensitive_by_table: dict[str, list[str]] = {}
            for table_name in tables:
                table = catalog.tables[table_name]
                definitions = []
                for column in table.columns:
                    suffix = " NOT NULL" if not column.nullable else ""
                    note = " /* 敏感字段：明确查询会被拒绝，结果按角色脱敏 */" if column.sensitive else ""
                    definitions.append(f'  {self._public_identifier(column.name)} {column.data_type}{suffix}{note}')
                    if column.sensitive:
                        sensitive_by_table.setdefault(table_name, []).append(column.name)
                ddl_parts.append(
                    f'CREATE TABLE {self._public_identifier(table_name)} (\n'
                    + ",\n".join(definitions)
                    + "\n);"
                )
            self.audit_log.allowed(role_policy.name, "get_schema", tables)
            return {
                "ddl": "\n\n".join(ddl_parts),
                "tables": tables,
                "sensitive_columns": sensitive_by_table,
                "error": None,
            }

    def validate_sql(self, *, sql: str, role: str = "") -> dict[str, Any]:
        with self._connection() as conn:
            catalog = self._catalog(conn)
            role_policy, checked, refs = self._validate(sql, role, catalog)
            effective_role = role_policy.name if role_policy else role
            if not checked.ok:
                self.audit_log.denied(effective_role, "validate_sql", checked.tables, checked.code, checked.message)
                result = checked.to_dict()
                result["sensitive_columns"] = [f"{ref.table}.{ref.column}" for ref in refs]
                return result
            self.audit_log.allowed(effective_role, "validate_sql", checked.tables)
            return checked.to_dict()

    def query(self, *, sql: str, role: str = "") -> dict[str, Any]:
        query_id = uuid.uuid4().hex
        try:
            with self._connection() as conn:
                catalog = self._catalog(conn)
                role_policy, checked, _refs = self._validate(sql, role, catalog)
                effective_role = role_policy.name if role_policy else role
                if not checked.ok:
                    self.audit_log.denied(effective_role, "query", checked.tables, checked.code, checked.message, query_id)
                    return _error(checked.code, checked.message, tables=checked.tables, query_id=query_id)
                cursor = conn.execute(checked.normalized_sql)
                columns = [description.name for description in (cursor.description or [])]
                raw_rows = cursor.fetchmany(self.max_rows + 1)
                truncated = len(raw_rows) > self.max_rows
                raw_rows = raw_rows[: self.max_rows]
                sensitive = {
                    column.name.lower()
                    for table_name in checked.tables
                    for column in catalog.tables.get(table_name, TableDescriptor(table_name, ())).columns
                    if column.sensitive
                }
                raw_columns = {
                    column
                    for table_name in checked.tables
                    for column in catalog.tables.get(table_name, TableDescriptor(table_name, ())).sensitive_columns
                    if role_policy is not None and role_policy.may_return_raw(table_name, column)
                }
                shaped_columns, rows = mask_sensitive_rows(
                    columns,
                    raw_rows,
                    sensitive_columns=sensitive,
                    raw_columns=raw_columns,
                )
                self.audit_log.allowed(effective_role, "query", checked.tables, query_id)
                return {
                    "columns": shaped_columns,
                    "rows": rows,
                    "truncated": truncated,
                    "query_id": query_id,
                    "tables": checked.tables,
                    "error": None,
                }
        except Exception as exc:  # database errors are structured for Agent correction
            message = f"{type(exc).__name__}: {exc}"
            self.audit_log.denied(role, "query", [], "DATABASE_ERROR", message, query_id)
            return _error("DATABASE_ERROR", message, retryable=False, query_id=query_id)

    def list_tables(self, *, role: str = "") -> dict[str, Any]:
        with self._connection() as conn:
            catalog = self._catalog(conn)
            role_policy = self._role(role)
            if role_policy is None:
                message = f"unknown role: {role!r}"
                self.audit_log.denied(role, "list_tables", [], "ACCESS_DENIED", message)
                return {"tables": [], "total_rows": 0, "error": {"code": "ACCESS_DENIED", "message": message}}
            result_tables: list[dict[str, Any]] = []
            total_rows = 0
            for table_name in self._role_tables(role_policy, catalog):
                table = catalog.tables[table_name]
                count = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
                count = int(count)
                total_rows += count
                result_tables.append({
                    "name": table_name,
                    "columns": [
                        {"name": column.name, "type": column.data_type, "sensitive": column.sensitive}
                        for column in table.columns
                    ],
                    "column_count": len(table.columns),
                    "row_count": count,
                })
            self.audit_log.allowed(role_policy.name, "list_tables", [item["name"] for item in result_tables])
            return {"tables": result_tables, "total_rows": total_rows, "error": None}

    def _check_browse_table(self, role: str, table_name: str, catalog: _Catalog) -> tuple[RolePolicy | None, TableDescriptor | None, dict[str, Any] | None]:
        role_policy = self._role(role)
        if role_policy is None:
            message = f"unknown role: {role!r}"
            return None, None, {"error": {"code": "ACCESS_DENIED", "message": message}}
        normalized = table_name.lower().strip()
        table = catalog.tables.get(normalized)
        if table is None or not role_policy.may_access_table(normalized):
            message = f"role does not have access to table: {table_name}"
            return role_policy, None, {"error": {"code": "TABLE_NOT_ALLOWED", "message": message}}
        return role_policy, table, None

    def _page_query(
        self,
        conn: Any,
        *,
        role_policy: RolePolicy,
        table: TableDescriptor,
        where_sql: str = "",
        where_params: Sequence[Any] = (),
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = min(MAX_PAGE_SIZE, max(1, int(page_size)))
        columns = list(table.column_names)
        quoted_columns = ", ".join(self._public_identifier(column) for column in columns)
        count_cursor = conn.execute(
            f'SELECT COUNT(*) FROM {self._public_identifier(table.name)} {where_sql}',
            tuple(where_params),
        )
        total_rows = int(count_cursor.fetchone()[0])
        offset = (page - 1) * page_size
        cursor = conn.execute(
            f'SELECT {quoted_columns} FROM {self._public_identifier(table.name)} {where_sql} '
            f'ORDER BY {self._public_identifier(columns[0])} LIMIT %s OFFSET %s',
            tuple(where_params) + (page_size, offset),
        )
        raw_rows = cursor.fetchall()
        sensitive = table.sensitive_columns
        raw_columns = {
            column
            for column in sensitive
            if role_policy.may_return_raw(table.name, column)
        }
        shaped_columns, rows = mask_sensitive_rows(
            columns,
            raw_rows,
            sensitive_columns=sensitive,
            raw_columns=raw_columns,
        )
        return {
            "table": table.name,
            "columns": shaped_columns,
            "rows": rows,
            "page": page,
            "page_size": page_size,
            "total_rows": total_rows,
            "total_pages": math.ceil(total_rows / page_size) if total_rows else 0,
            "sensitive_columns": sorted(sensitive),
            "error": None,
        }

    def browse_table(
        self,
        *,
        table: str,
        role: str = "",
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        with self._connection() as conn:
            catalog = self._catalog(conn)
            role_policy, descriptor, error = self._check_browse_table(role, table, catalog)
            if error:
                self.audit_log.denied(role, "browse_table", [table], error["error"]["code"], error["error"]["message"])
                return error
            assert role_policy is not None and descriptor is not None
            result = self._page_query(conn, role_policy=role_policy, table=descriptor, page=page, page_size=page_size)
            self.audit_log.allowed(role_policy.name, "browse_table", [descriptor.name])
            return result

    def search_table(
        self,
        *,
        table: str,
        term: str,
        role: str = "",
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        term = (term or "").strip()[:200]
        with self._connection() as conn:
            catalog = self._catalog(conn)
            role_policy, descriptor, error = self._check_browse_table(role, table, catalog)
            if error:
                self.audit_log.denied(role, "search_table", [table], error["error"]["code"], error["error"]["message"])
                return error
            assert role_policy is not None and descriptor is not None
            text_columns = [
                column for column in descriptor.columns
                if column.data_type.lower() in {"text", "character varying", "character"}
                and not column.sensitive
            ]
            if not text_columns or not term:
                return self._page_query(conn, role_policy=role_policy, table=descriptor, page=page, page_size=page_size)
            clauses = " OR ".join(f'"{column.name}"::text ILIKE %s' for column in text_columns)
            params = tuple(f"%{term}%" for _ in text_columns)
            result = self._page_query(
                conn,
                role_policy=role_policy,
                table=descriptor,
                where_sql=f"WHERE {clauses}",
                where_params=params,
                page=page,
                page_size=page_size,
            )
            result["search_term"] = term
            self.audit_log.allowed(role_policy.name, "search_table", [descriptor.name])
            return result

    @staticmethod
    def _to_csv(columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows(rows)
        return output.getvalue()

    def export_table_csv(
        self,
        *,
        table: str,
        role: str = "",
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """Export one bounded, role-filtered table page as UTF-8 CSV."""
        with self._connection() as conn:
            catalog = self._catalog(conn)
            role_policy, descriptor, error = self._check_browse_table(role, table, catalog)
            if error:
                self.audit_log.denied(role, "export_table_csv", [table], error["error"]["code"], error["error"]["message"])
                return error
            assert role_policy is not None and descriptor is not None
            result = self._page_query(
                conn,
                role_policy=role_policy,
                table=descriptor,
                page=page,
                page_size=page_size,
            )
            result["csv"] = self._to_csv(result["columns"], result["rows"])
            result["filename"] = f"{descriptor.name}-page-{result['page']}.csv"
            self.audit_log.allowed(role_policy.name, "export_table_csv", [descriptor.name])
            return result

    def export_query_csv(self, *, sql: str, role: str = "") -> dict[str, Any]:
        """Export a policy-approved query result as bounded UTF-8 CSV."""
        result = self.query(sql=sql, role=role)
        if result.get("error") is not None:
            return result
        result["csv"] = self._to_csv(result["columns"], result["rows"])
        result["filename"] = "queryagent-result.csv"
        return result

    def search_values(self, *, term: str, role: str = "", limit: int = 5) -> dict[str, Any]:
        term = (term or "").strip()[:200]
        limit = min(20, max(1, int(limit)))
        with self._connection() as conn:
            catalog = self._catalog(conn)
            role_policy = self._role(role)
            if role_policy is None:
                message = f"unknown role: {role!r}"
                self.audit_log.denied(role, "search_values", [], "ACCESS_DENIED", message)
                return {"matches": [], "error": {"code": "ACCESS_DENIED", "message": message}}
            allowed_tables = self._role_tables(role_policy, catalog)
            if not allowed_tables:
                return {"matches": [], "error": None}

            # Never use a sensitive value as model context unless the current
            # role explicitly has raw access to that table/column. This keeps
            # value retrieval consistent with query and table browsing.
            visibility_clauses = ["NOT sensitive"]
            visibility_params: list[str] = []
            for table_name in allowed_tables:
                descriptor = catalog.tables[table_name]
                for column_name in descriptor.sensitive_columns:
                    if role_policy.may_return_raw(table_name, column_name):
                        visibility_clauses.append(
                            "(sensitive AND table_name = %s AND column_name = %s)"
                        )
                        visibility_params.extend([table_name, column_name])
            table_placeholders = ", ".join(["%s"] * len(allowed_tables))
            rows = conn.execute(
                f"""
                SELECT table_name, column_name, value,
                       similarity(value, %s) AS similarity
                FROM value_index
                WHERE table_name IN ({table_placeholders})
                  AND (value %% %s OR value ILIKE %s)
                  AND ({" OR ".join(visibility_clauses)})
                ORDER BY similarity DESC, table_name, column_name, value
                LIMIT %s
                """,
                (
                    term,
                    *allowed_tables,
                    term,
                    f"%{term}%",
                    *visibility_params,
                    limit,
                ),
            ).fetchall()
            matches = [
                {
                    "table_name": str(table_name),
                    "column_name": str(column_name),
                    "value": str(value),
                    "similarity": round(float(similarity), 3),
                }
                for table_name, column_name, value, similarity in rows
            ]
            self.audit_log.allowed(role_policy.name, "search_values", allowed_tables)
            return {"matches": matches, "error": None}


def catalog_from_descriptors(descriptors: Sequence[TableDescriptor]) -> _Catalog:
    """Small test/adapter helper for injecting a catalog without PostgreSQL."""
    return _Catalog({descriptor.name.lower(): descriptor for descriptor in descriptors})
