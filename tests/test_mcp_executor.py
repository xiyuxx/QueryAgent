from queryagent.eval.sample_db import build_sample_db
from queryagent.tools.mcp_client import MCPExecutor


def test_mcp_contract_enforces_policy_and_returns_schema(tmp_path) -> None:
    db_path = build_sample_db(tmp_path / "sales.db")
    with MCPExecutor(db_path, timeout_s=20) as executor:
        schema = executor.get_schema()
        assert "customers" in schema["tables"]

        allowed = executor.validate_sql("SELECT city FROM customers")
        assert allowed.ok
        assert allowed.tables == ["customers"]

        denied = executor.validate_sql("WITH changed AS (DELETE FROM customers RETURNING *) SELECT * FROM changed")
        assert not denied.ok
        assert denied.code == "POLICY_DENIED"

        result = executor.execute("SELECT COUNT(*) FROM customers")
        assert result.error is None
        assert result.rows == [(5,)]

        blocked = executor.execute("DELETE FROM customers")
        assert blocked.error is not None
        assert "POLICY_DENIED" in blocked.error
