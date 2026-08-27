"""MCP end-to-end smoke test for the controlled SQLite data server.

Usage:
    python -m scripts.test_mcp
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "src"))

from queryagent.eval.sample_db import build_sample_db
from queryagent.tools.mcp_client import MCPExecutor


def main() -> None:
    db_path = build_sample_db(ROOT / "data" / "sales.db")
    with MCPExecutor(db_path) as executor:
        schema = executor.get_schema()
        print("tables:", schema["tables"])

        validation = executor.validate_sql("SELECT city, COUNT(*) FROM customers GROUP BY city")
        print("validation:", validation.to_dict())

        result = executor.execute("SELECT city, COUNT(*) AS n FROM customers GROUP BY city")
        print("query:", result)

        blocked = executor.execute("WITH changed AS (DELETE FROM customers RETURNING *) SELECT * FROM changed")
        print("write_cte:", blocked.error)


if __name__ == "__main__":
    main()
