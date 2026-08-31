from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re

import pytest

from queryagent.tools.access import AccessConfig, RolePolicy
from queryagent.tools.postgres import (
    ColumnDescriptor,
    PostgresDataService,
    TableDescriptor,
    catalog_from_descriptors,
)
from queryagent.tools.postgres_mcp_server import build_postgres_server


@dataclass
class FakeDescription:
    name: str


class FakeResult:
    def __init__(self, rows=(), description=()):
        self._rows = list(rows)
        self.description = [FakeDescription(name) for name in description]

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchmany(self, size):
        return list(self._rows[:size])


class FakeConnection:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self.closed = False
        self._catalog_rows = [
            ("customers", "customer_id", "bigint", "NO", 1),
            ("customers", "name", "text", "NO", 2),
            ("customers", "email", "text", "NO", 3),
            ("orders", "order_id", "bigint", "NO", 1),
        ]
        self._data = {
            "customers": [
                (1, "张伟", "a@example.com"),
                (2, "李娜", "b@example.com"),
                (3, "王强", "c@example.com"),
            ],
            "orders": [(1,)],
        }

    def execute(self, statement, params=None):
        params_tuple = tuple(params or ())
        self.calls.append((str(statement), params_tuple))
        sql = str(statement)
        if "FROM information_schema.columns" in sql:
            return FakeResult(self._catalog_rows)
        if "FROM schema_metadata" in sql:
            return FakeResult([])
        if "SELECT COUNT(*)" in sql:
            if "customers" in sql and "WHERE" not in sql:
                return FakeResult([(len(self._data["customers"]),)])
            if "orders" in sql:
                return FakeResult([(len(self._data["orders"]),)])
            return FakeResult([(0,)])
        if "FROM customers" in sql or 'FROM "customers"' in sql:
            rows = self._data["customers"]
            if "WHERE" in sql:
                terms = [
                    value.strip("%")
                    for value in params_tuple
                    if isinstance(value, str) and value.startswith("%")
                ]
                rows = [row for row in rows if any(term in row[1] for term in terms)]
            integer_params = [value for value in params_tuple if isinstance(value, int)]
            if integer_params:
                limit = integer_params[0]
                offset = integer_params[1] if len(integer_params) > 1 else 0
                rows = rows[offset:offset + limit]
            if re.search(r"SELECT\s+email\s+FROM", sql, re.IGNORECASE):
                rows = [(row[2],) for row in rows]
                return FakeResult(rows, ("email",))
            return FakeResult(rows, ("customer_id", "name", "email"))
        if "FROM orders" in sql or 'FROM "orders"' in sql:
            rows = self._data["orders"]
            integer_params = [value for value in params_tuple if isinstance(value, int)]
            if integer_params:
                rows = rows[:integer_params[0]]
            return FakeResult(rows, ("order_id",))
        return FakeResult([])

    def close(self):
        self.closed = True

    def rollback(self):
        pass


@pytest.fixture
def service_and_connection():
    connection = FakeConnection()
    catalog = catalog_from_descriptors(
        [
            TableDescriptor(
                "customers",
                (
                    ColumnDescriptor("customer_id", "bigint", nullable=False),
                    ColumnDescriptor("name", "text", nullable=False),
                    ColumnDescriptor("email", "text", nullable=False, sensitive=True),
                ),
            ),
            TableDescriptor(
                "orders",
                (ColumnDescriptor("order_id", "bigint", nullable=False),),
            ),
        ]
    )
    config = AccessConfig(
        roles={
            "readonly": RolePolicy(name="readonly"),
            "admin": RolePolicy(name="admin"),
            "analyst": RolePolicy(name="analyst", allowed_tables={"customers"}),
        },
        default_role="readonly",
    )
    service = PostgresDataService(
        "postgresql://reader@db/queryagent_demo",
        max_rows=2,
        access_config=config,
        connection_factory=lambda _dsn: connection,
        catalog_loader=lambda _conn: catalog,
    )
    return service, connection


def test_service_query_masks_select_star_and_enforces_max_rows(service_and_connection):
    service, connection = service_and_connection

    result = service.query(sql="SELECT * FROM customers", role="readonly")

    assert result["error"] is None
    assert result["columns"] == ["customer_id", "name", "email"]
    assert result["rows"] == [[1, "张伟", "******"], [2, "李娜", "******"]]
    assert result["truncated"] is True
    assert any("SET default_transaction_read_only" in statement for statement, _ in connection.calls)


def test_service_rejects_explicit_sensitive_column_before_database_query(service_and_connection):
    service, connection = service_and_connection
    before = len(connection.calls)

    result = service.query(sql="SELECT email FROM customers", role="readonly")

    assert result["error"]["code"] == "SENSITIVE_COLUMN"
    assert len(connection.calls) > before
    assert not any("SELECT email FROM" in statement for statement, _ in connection.calls)


def test_service_allows_admin_sensitive_column(service_and_connection):
    service, _connection = service_and_connection

    result = service.query(sql="SELECT email FROM customers", role="admin")

    assert result["error"] is None
    assert result["columns"] == ["email"]
    assert result["rows"][0][0] == "a@example.com"


def test_service_enforces_role_table_scope_and_unknown_role(service_and_connection):
    service, _connection = service_and_connection

    denied = service.query(sql="SELECT * FROM orders", role="analyst")
    unknown = service.list_tables(role="missing")

    assert denied["error"]["code"] == "TABLE_NOT_ALLOWED"
    assert unknown["error"]["code"] == "ACCESS_DENIED"


def test_service_browse_search_and_csv_are_role_filtered(service_and_connection):
    service, _connection = service_and_connection

    page = service.browse_table(table="customers", role="readonly", page=2, page_size=1)
    searched = service.search_table(table="customers", term="李", role="readonly", page_size=50)
    exported = service.export_table_csv(table="customers", role="readonly", page_size=1)

    assert page["page"] == 2
    assert page["page_size"] == 1
    assert page["rows"] == [[2, "李娜", "******"]]
    assert searched["rows"] == [[2, "李娜", "******"]]
    assert "email" in exported["csv"].splitlines()[0]
    assert "******" in exported["csv"]
    assert "a@example.com" not in exported["csv"]


def test_service_schema_and_tables_hide_internal_catalog_entries(service_and_connection):
    service, _connection = service_and_connection

    schema = service.get_schema(role="readonly")
    tables = service.list_tables(role="readonly")

    assert schema["tables"] == ["customers", "orders"]
    assert {item["name"] for item in tables["tables"]} == {"customers", "orders"}
    assert "schema_metadata" not in schema["ddl"]


def test_mcp_server_registers_query_and_data_tools(service_and_connection):
    service, _connection = service_and_connection
    server = build_postgres_server(
        "postgresql://reader@db/queryagent_demo",
        service=service,
    )

    tools = {tool.name for tool in server._tool_manager.list_tools()}

    assert tools == {
        "get_schema",
        "search_values",
        "validate_sql",
        "query",
        "list_tables",
        "browse_table",
        "search_table",
        "export_table_csv",
        "export_query_csv",
    }


def test_explicit_composite_row_and_json_requests_are_detected():
    catalog = catalog_from_descriptors(
        [
            TableDescriptor(
                "customers",
                (
                    ColumnDescriptor("customer_id", "bigint"),
                    ColumnDescriptor("email", "text", sensitive=True),
                ),
            )
        ]
    )
    for sql in (
        "SELECT c FROM customers c",
        "SELECT row_to_json(c) FROM customers c",
        "SELECT to_jsonb(customers) FROM customers",
        "SELECT (customers).* FROM customers",
    ):
        refs = __import__(
            "queryagent.tools.postgres", fromlist=["find_sensitive_references"]
        ).find_sensitive_references(sql, catalog)
        assert [(ref.table, ref.column) for ref in refs] == [("customers", "email")]
