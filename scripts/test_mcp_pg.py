"""PostgreSQL MCP smoke test for a running local Compose stack.

Run after ``docker compose up --build`` and database initialization:

    python scripts/test_mcp_pg.py

The script intentionally uses the configured reader DSN and verifies the MCP
stdio boundary, role-visible schema, row pagination, sensitive-field masking,
and read-only rejection.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from queryagent.tools.mcp_client import MCPExecutor


def main() -> None:
    dsn = os.environ.get("QUERYAGENT_MCP_DSN")
    if not dsn:
        raise SystemExit("QUERYAGENT_MCP_DSN is required")
    with MCPExecutor(dsn, timeout_s=30, role="readonly") as executor:
        schema = executor.get_schema()
        assert schema.get("error") is None, schema
        assert "customers" in schema["tables"], schema
        assert "schema_metadata" not in schema["tables"], schema

        page = executor.browse_table("customers", page=1, page_size=50)
        assert page.get("error") is None, page
        assert page["page_size"] == 50, page
        assert "phone" in page["columns"], page
        assert page["rows"][0][page["columns"].index("phone")] == "******", page

        denied = executor.validate_sql("SELECT email FROM customers")
        assert not denied.ok, denied
        assert denied.code == "SENSITIVE_COLUMN", denied

        write = executor.execute("DELETE FROM customers")
        assert write.error and "POLICY_DENIED" in write.error, write
    print("PostgreSQL MCP smoke test: ok")


if __name__ == "__main__":
    main()
