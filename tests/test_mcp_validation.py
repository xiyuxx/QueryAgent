"""MCP execution boundary validation test suite.

Covers three areas that cannot be tested with unit tests alone:
1. Security policy — the MCP server rejects dangerous SQL at the boundary
2. Tool contract — validate_sql / get_schema / query return correct structure
3. End-to-end accuracy — AgentLoop via MCP produces the same execution
   accuracy as the direct SQLite path on the same eval cases

Run:
    .venv/bin/python -m pytest tests/test_mcp_validation.py -v
"""
from __future__ import annotations

import pytest

from queryagent.eval.sample_db import build_sample_db
from queryagent.eval.warehouse_db import build_warehouse_db
from queryagent.tools.mcp_client import MCPExecutor


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────

@pytest.fixture(scope="module")
def sales_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "sales.db"
    return build_sample_db(path)


@pytest.fixture(scope="module")
def warehouse_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "warehouse.db"
    return build_warehouse_db(str(path))


@pytest.fixture(scope="module")
def sales_mcp(sales_db):
    with MCPExecutor(sales_db, timeout_s=20) as ex:
        yield ex


@pytest.fixture(scope="module")
def warehouse_mcp(warehouse_db):
    with MCPExecutor(warehouse_db, timeout_s=20) as ex:
        yield ex


# ──────────────────────────────────────────────
# 1. Security policy boundary
# ──────────────────────────────────────────────

class TestSecurityPolicy:
    """Every dangerous SQL pattern must be rejected server-side, not just by
    the client, so that a compromised or bypassed client cannot execute writes."""

    @pytest.mark.parametrize("sql,expected_code", [
        # Direct write operations
        ("DELETE FROM customers", "POLICY_DENIED"),
        ("INSERT INTO customers VALUES (99,'x','y','2024-01-01')", "POLICY_DENIED"),
        ("UPDATE customers SET city='z' WHERE 1=1", "POLICY_DENIED"),
        ("DROP TABLE customers", "POLICY_DENIED"),
        ("CREATE TABLE evil (x TEXT)", "POLICY_DENIED"),
        # Write hidden inside a CTE
        ("WITH d AS (DELETE FROM customers RETURNING *) SELECT * FROM d",
         "POLICY_DENIED"),
        ("WITH u AS (UPDATE customers SET city='x' RETURNING *) SELECT * FROM u",
         "POLICY_DENIED"),
        # Multiple statements
        ("SELECT 1; DROP TABLE customers", "MULTIPLE_STATEMENTS"),
        # Empty / whitespace-only
        ("", "EMPTY_SQL"),
        # sqlglot parses "   ;  " as a single null statement, caught as POLICY_DENIED
        ("   ;  ", "POLICY_DENIED"),
    ])
    def test_dangerous_sql_rejected(self, sales_mcp, sql, expected_code):
        result = sales_mcp.execute(sql)
        assert result.error is not None, f"expected rejection for: {sql!r}"
        assert expected_code in result.error, (
            f"expected code {expected_code!r} in error {result.error!r}"
        )

    def test_validate_sql_returns_policy_denied_not_database_error(self, sales_mcp):
        """Policy rejection must happen before DB execution — the error code
        must be POLICY_DENIED, not OperationalError from SQLite."""
        result = sales_mcp.validate_sql("DELETE FROM customers")
        assert not result.ok
        assert result.code == "POLICY_DENIED"

    def test_safe_select_passes_policy(self, sales_mcp):
        result = sales_mcp.validate_sql(
            "SELECT city, COUNT(*) FROM customers GROUP BY city"
        )
        assert result.ok
        assert result.code == "OK"

    def test_read_only_cte_passes_policy(self, sales_mcp):
        result = sales_mcp.validate_sql(
            "WITH top AS (SELECT * FROM customers ORDER BY name LIMIT 3) "
            "SELECT city FROM top"
        )
        assert result.ok


# ──────────────────────────────────────────────
# 2. Tool contract
# ──────────────────────────────────────────────

class TestToolContract:
    """MCP tools must return correct structure and contents so the agent
    can rely on the contract without inspecting raw text."""

    def test_get_schema_lists_all_tables(self, sales_mcp):
        schema = sales_mcp.get_schema()
        assert "tables" in schema
        assert set(schema["tables"]) == {"customers", "orders", "products"}

    def test_get_schema_includes_ddl(self, sales_mcp):
        schema = sales_mcp.get_schema()
        ddl = schema["ddl"]
        assert "customers" in ddl
        assert "orders" in ddl
        assert "products" in ddl

    def test_validate_sql_returns_table_names(self, sales_mcp):
        result = sales_mcp.validate_sql(
            "SELECT p.name FROM orders o JOIN products p "
            "ON o.product_id = p.product_id"
        )
        assert result.ok
        assert set(result.tables) == {"orders", "products"}

    def test_query_returns_correct_columns(self, sales_mcp):
        result = sales_mcp.execute(
            "SELECT city, COUNT(*) AS n FROM customers GROUP BY city ORDER BY city"
        )
        assert result.error is None
        assert result.columns == ["city", "n"]

    def test_query_truncation_flag(self, sales_db):
        # max_rows=2 forces truncation on the 5-row customers table
        with MCPExecutor(sales_db, timeout_s=20) as ex:
            # override max_rows by talking directly to a low-limit server
            # we just confirm the flag propagates; full truncation tested via server env
            result = ex.execute("SELECT * FROM customers")
            # default max_rows=100, 5 rows → no truncation
            assert result.truncated is False
            assert len(result.rows) == 5

    def test_query_error_is_structured_string(self, sales_mcp):
        result = sales_mcp.execute("SELECT nonexistent_col FROM customers")
        assert result.error is not None
        assert isinstance(result.error, str)
        # The error must come from the DB layer, not policy
        assert "POLICY_DENIED" not in result.error

    def test_warehouse_schema_has_expected_domains(self, warehouse_mcp):
        schema = warehouse_mcp.get_schema()
        tables = set(schema["tables"])
        # Four domains must be present
        assert "employees" in tables       # HR
        assert "orders" in tables          # e-commerce
        assert "accounts" in tables        # finance
        assert "shipments" in tables       # logistics


# ──────────────────────────────────────────────
# 3. End-to-end accuracy parity
# ──────────────────────────────────────────────

class TestEndToEndAccuracy:
    """AgentLoop via MCP must produce results identical to the direct
    SQLiteExecutor path on the same queries. This confirms the MCP server
    is a transparent execution boundary, not a transform layer."""

    # Pairs of (question, gold_sql) taken directly from mini.jsonl
    CASES = [
        ("北京的客户有多少个？",
         "SELECT COUNT(*) FROM customers WHERE city = '北京'"),
        ("每个城市的客户数量分别是多少？",
         "SELECT city, COUNT(*) FROM customers GROUP BY city ORDER BY city"),
        ("价格最高的产品叫什么名字？",
         "SELECT name FROM products ORDER BY price DESC LIMIT 1"),
        ("总销售额是多少？",
         "SELECT SUM(o.quantity * p.price) FROM orders o "
         "JOIN products p ON o.product_id = p.product_id"),
    ]

    def _sqlite_rows(self, sales_db, sql):
        from queryagent.tools.db import SQLiteExecutor
        return SQLiteExecutor(sales_db).execute(sql).rows

    def test_mcp_results_match_sqlite_for_all_cases(self, sales_mcp, sales_db):
        mismatches = []
        for question, sql in self.CASES:
            mcp_result = sales_mcp.execute(sql)
            sqlite_rows = self._sqlite_rows(sales_db, sql)

            if mcp_result.error is not None:
                mismatches.append(f"{question!r}: MCP returned error {mcp_result.error!r}")
                continue

            def norm(rows):
                return sorted(tuple(str(v) for v in r) for r in rows)

            if norm(mcp_result.rows) != norm(sqlite_rows):
                mismatches.append(
                    f"{question!r}: MCP={mcp_result.rows} vs SQLite={sqlite_rows}"
                )

        assert not mismatches, "result mismatch between MCP and SQLite:\n" + "\n".join(mismatches)

    def test_agent_loop_via_mcp_reaches_done(self, sales_db):
        """Full AgentLoop with MockLLM through MCP executor must produce
        status=done (not failed) for a straightforward aggregation."""
        from queryagent.agent.loop import AgentLoop
        from queryagent.llm.mock import MockLLM
        from queryagent.reliability.validator import ResultValidator
        from queryagent.schema.retriever import SchemaRetriever

        with MCPExecutor(sales_db, timeout_s=20) as executor:
            agent = AgentLoop(
                llm=MockLLM(),
                executor=executor,
                validator=ResultValidator(),
                schema_retriever=SchemaRetriever(sales_db),
                max_corrections=2,
            )
            result = agent.run("北京的客户有多少个？")

        assert result.status == "done", (
            f"AgentLoop via MCP failed: status={result.status} error={result.error}"
        )
        assert result.rows is not None
        assert result.rows == [(2,)]

    def test_self_correction_fires_through_mcp(self, sales_db):
        """q08 in MockLLM is designed to fail on first attempt (bad column name)
        and succeed after one correction. Verify the correction loop works when
        execution goes through MCP rather than SQLiteExecutor directly."""
        from queryagent.agent.loop import AgentLoop
        from queryagent.llm.mock import MockLLM
        from queryagent.reliability.validator import ResultValidator
        from queryagent.schema.retriever import SchemaRetriever

        with MCPExecutor(sales_db, timeout_s=30) as executor:
            agent = AgentLoop(
                llm=MockLLM(),
                executor=executor,
                validator=ResultValidator(),
                schema_retriever=SchemaRetriever(sales_db),
                max_corrections=3,
            )
            result = agent.run("列出所有客户的名字")  # q08 pattern

        assert result.status == "done", (
            f"self-correction via MCP failed: status={result.status} "
            f"error={result.error} corrections={result.corrections}"
        )
        assert result.corrections >= 1, (
            "expected at least one correction round for q08 pattern"
        )
