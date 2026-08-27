from queryagent.tools.policy import SQLPolicy


def test_allows_single_read_query() -> None:
    result = SQLPolicy().validate("SELECT name FROM customers WHERE city = '北京'")
    assert result.ok
    assert result.tables == ["customers"]


def test_allows_semicolon_inside_literal() -> None:
    result = SQLPolicy().validate("SELECT ';' AS marker")
    assert result.ok


def test_rejects_multiple_statements() -> None:
    result = SQLPolicy().validate("SELECT 1; SELECT 2")
    assert not result.ok
    assert result.code == "MULTIPLE_STATEMENTS"


def test_rejects_write_cte() -> None:
    result = SQLPolicy().validate("WITH changed AS (DELETE FROM customers RETURNING *) SELECT * FROM changed")
    assert not result.ok
    assert result.code == "POLICY_DENIED"


def test_enforces_table_allowlist() -> None:
    result = SQLPolicy(allowed_tables={"customers"}).validate("SELECT * FROM orders")
    assert not result.ok
    assert result.code == "TABLE_NOT_ALLOWED"


def test_rejects_denied_function() -> None:
    result = SQLPolicy(dialect="postgres").validate("SELECT pg_sleep(10)")
    assert not result.ok
    assert result.code == "POLICY_DENIED"
