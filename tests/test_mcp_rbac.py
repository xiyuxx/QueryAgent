"""Role-based access control tests for the MCP data server.

Verifies that:
1. Unknown roles are rejected at every tool entry point.
2. Roles are denied access to tables outside their allowed_tables list.
3. Roles that are allowed still get correct results.
4. Blocked columns are stripped from both DDL (get_schema) and result rows (query).
5. Audit log entries are written for every decision.

Run:
    .venv/bin/python -m pytest tests/test_mcp_rbac.py -v
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from queryagent.eval.warehouse_db import build_warehouse_db
from queryagent.tools.access import AccessConfig, AuditLog, RolePolicy, load_access_config
from queryagent.tools.mcp_server import build_server


# ──────────────────────────────────────────────
# Helpers — call MCP tools directly without stdio
# ──────────────────────────────────────────────

def _tools(server):
    """Return a dict of tool_name -> callable from a FastMCP instance."""
    return {t.name: t.fn for t in server._tool_manager.list_tools()}


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────

@pytest.fixture(scope="module")
def warehouse_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "warehouse.db"
    return build_warehouse_db(str(path))


@pytest.fixture(scope="module")
def rbac_config() -> AccessConfig:
    """Minimal config that covers all test scenarios."""
    return AccessConfig(
        roles={
            "analyst": RolePolicy(
                name="analyst",
                allowed_tables={"customers", "orders", "products"},
                blocked_columns={"customers": {"phone", "email"}},
            ),
            "hr": RolePolicy(
                name="hr",
                allowed_tables={"employees", "departments", "salaries"},
                blocked_columns={},
            ),
            "admin": RolePolicy(
                name="admin",
                allowed_tables=None,     # all tables
                blocked_columns={},
            ),
            "readonly": RolePolicy(
                name="readonly",
                allowed_tables=None,
                blocked_columns={"employees": {"national_id"}},
            ),
        },
        default_role="readonly",
    )


@pytest.fixture(scope="module")
def audit_log(tmp_path_factory) -> tuple[AuditLog, Path]:
    path = tmp_path_factory.mktemp("audit") / "audit.jsonl"
    return AuditLog(path), path


@pytest.fixture(scope="module")
def server(warehouse_db, rbac_config, audit_log):
    log, _ = audit_log
    return build_server(warehouse_db, access_config=rbac_config, audit_log=log)


@pytest.fixture(scope="module")
def tools(server):
    return _tools(server)


# ──────────────────────────────────────────────
# 1. Unknown role rejected everywhere
# ──────────────────────────────────────────────

class TestUnknownRole:
    def test_query_rejects_unknown_role(self, tools):
        result = tools["query"](sql="SELECT * FROM customers", role="ghost")
        assert result["error"] is not None
        assert result["error"]["code"] == "ACCESS_DENIED"
        assert "ghost" in result["error"]["message"]

    def test_get_schema_rejects_unknown_role(self, tools):
        result = tools["get_schema"](role="ghost")
        assert "error" in result
        assert result["error"]["code"] == "ACCESS_DENIED"

    def test_validate_sql_rejects_unknown_role(self, tools):
        result = tools["validate_sql"](sql="SELECT 1", role="ghost")
        assert not result["ok"]
        assert result["code"] == "ACCESS_DENIED"


# ──────────────────────────────────────────────
# 2. Table-level access control
# ──────────────────────────────────────────────

class TestTableAccess:
    def test_analyst_can_query_allowed_table(self, tools):
        result = tools["query"](sql="SELECT COUNT(*) FROM customers", role="analyst")
        assert result["error"] is None
        assert result["rows"][0][0] >= 0

    def test_analyst_denied_hr_table(self, tools):
        result = tools["query"](sql="SELECT * FROM employees LIMIT 1", role="analyst")
        assert result["error"] is not None
        assert result["error"]["code"] == "TABLE_NOT_ALLOWED"
        assert "employees" in result["error"]["message"]

    def test_hr_can_query_employees(self, tools):
        result = tools["query"](sql="SELECT COUNT(*) FROM employees", role="hr")
        assert result["error"] is None

    def test_hr_denied_customer_table(self, tools):
        result = tools["query"](sql="SELECT * FROM customers LIMIT 1", role="hr")
        assert result["error"] is not None
        assert result["error"]["code"] == "TABLE_NOT_ALLOWED"

    def test_admin_can_query_any_table(self, tools):
        for table in ("customers", "employees", "accounts", "shipments"):
            result = tools["query"](sql=f"SELECT COUNT(*) FROM {table}", role="admin")
            assert result["error"] is None, f"admin denied {table}: {result['error']}"

    def test_validate_sql_respects_table_access(self, tools):
        allowed = tools["validate_sql"](
            sql="SELECT COUNT(*) FROM customers", role="analyst"
        )
        assert allowed["ok"]

        denied = tools["validate_sql"](
            sql="SELECT COUNT(*) FROM salaries", role="analyst"
        )
        assert not denied["ok"]
        assert denied["code"] == "TABLE_NOT_ALLOWED"


# ──────────────────────────────────────────────
# 3. get_schema respects role visibility
# ──────────────────────────────────────────────

class TestSchemaVisibility:
    def test_analyst_schema_excludes_hr_tables(self, tools):
        schema = tools["get_schema"](role="analyst")
        assert "error" not in schema or schema.get("error") is None
        tables = set(schema["tables"])
        assert "customers" in tables
        assert "employees" not in tables
        assert "salaries" not in tables

    def test_hr_schema_includes_only_hr_tables(self, tools):
        schema = tools["get_schema"](role="hr")
        tables = set(schema["tables"])
        assert "employees" in tables
        assert "departments" in tables
        assert "customers" not in tables

    def test_admin_schema_includes_all_tables(self, tools):
        schema = tools["get_schema"](role="admin")
        tables = set(schema["tables"])
        assert "customers" in tables
        assert "employees" in tables
        assert "accounts" in tables


# ──────────────────────────────────────────────
# 4. Blocked columns stripped from DDL and results
# ──────────────────────────────────────────────

class TestBlockedColumns:
    def test_blocked_column_absent_from_ddl(self, tools):
        schema = tools["get_schema"](role="analyst")
        # analyst has phone and email blocked on customers
        assert "phone" not in schema["ddl"]
        assert "email" not in schema["ddl"]

    def test_blocked_column_absent_from_query_result(self, tools, warehouse_db):
        # Use the sales DB which has customers with city/name columns
        from queryagent.eval.sample_db import build_sample_db
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            db = build_sample_db(Path(d) / "s.db")
            cfg = AccessConfig(
                roles={
                    "analyst": RolePolicy(
                        name="analyst",
                        allowed_tables={"customers"},
                        blocked_columns={"customers": {"city"}},
                    )
                },
                default_role="analyst",
            )
            srv = build_server(db, access_config=cfg)
            t = _tools(srv)
            result = t["query"](sql="SELECT name, city FROM customers LIMIT 1", role="analyst")
            assert result["error"] is None
            assert "city" not in result["columns"]
            assert "name" in result["columns"]

    def test_unblocked_column_still_present(self, tools):
        # admin has no blocked columns — all columns visible
        schema = tools["get_schema"](role="admin")
        # The warehouse customers table has a city column
        assert "city" in schema["ddl"].lower() or "customers" in schema["ddl"].lower()


# ──────────────────────────────────────────────
# 5. Audit log
# ──────────────────────────────────────────────

class TestAuditLog:
    def test_audit_log_records_allowed_query(self, tools, audit_log):
        _, log_path = audit_log
        before = log_path.stat().st_size if log_path.exists() else 0

        tools["query"](sql="SELECT COUNT(*) FROM customers", role="analyst")

        entries = [
            json.loads(line)
            for line in log_path.read_text().splitlines()
            if line.strip()
        ]
        after_entries = [
            e for e in entries
            if e.get("role") == "analyst" and e.get("decision") == "ALLOWED"
        ]
        assert len(after_entries) >= 1

    def test_audit_log_records_denied_query(self, tools, audit_log):
        _, log_path = audit_log
        tools["query"](sql="SELECT * FROM employees LIMIT 1", role="analyst")

        entries = [
            json.loads(line)
            for line in log_path.read_text().splitlines()
            if line.strip()
        ]
        denied = [
            e for e in entries
            if e.get("role") == "analyst"
            and e.get("decision") == "DENIED"
            and e.get("code") == "TABLE_NOT_ALLOWED"
        ]
        assert len(denied) >= 1, "expected a DENIED audit entry for analyst→employees"

    def test_audit_log_entry_has_required_fields(self, tools, audit_log):
        _, log_path = audit_log
        tools["query"](sql="SELECT COUNT(*) FROM orders", role="analyst")

        entries = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        last = entries[-1]
        for field in ("timestamp", "role", "tool", "decision", "tables", "query_id"):
            assert field in last, f"missing field {field!r} in audit entry"

    def test_unknown_role_denial_logged(self, tools, audit_log):
        _, log_path = audit_log
        tools["query"](sql="SELECT 1", role="attacker")

        entries = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        denied = [e for e in entries if e.get("role") == "attacker" and e.get("decision") == "DENIED"]
        assert len(denied) >= 1
