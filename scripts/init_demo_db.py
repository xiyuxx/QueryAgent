"""Initialize the deterministic PostgreSQL demo database.

This command is run by the Compose ``db-init`` service. It is intentionally
separate from the FastAPI process: initialization/reset may connect directly
to PostgreSQL, while query-time access is kept behind the MCP boundary.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make ``python scripts/init_demo_db.py`` work from a source checkout as well
# as the installed backend image.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from queryagent.database.initializer import ensure_production_database


def main() -> None:
    dsn = os.environ.get("QUERYAGENT_DB_DSN")
    if not dsn:
        raise SystemExit("QUERYAGENT_DB_DSN is required")
    result = ensure_production_database(
        dsn,
        embedding_model=os.environ.get(
            "QUERYAGENT_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"
        ),
        embedding_dim=int(os.environ.get("QUERYAGENT_EMBEDDING_DIM", "512")),
    )
    print(
        "QueryAgent demo database ready: "
        f"{len(result.row_counts)} tables, {result.total_rows} rows, "
        f"seed={result.seed}, digest={result.snapshot_digest[:12]}"
    )


if __name__ == "__main__":
    main()
