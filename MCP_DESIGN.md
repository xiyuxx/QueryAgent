# MCP Execution Boundary

## Scope

MCP is the only execution boundary between the agent and a data source. The
agent may propose SQL, but it cannot access a DSN, a database path, resource
limits, or authorization settings.

## Tools

| Tool | Purpose | Server enforcement |
|---|---|---|
| `get_schema` | Return visible DDL and relevant tables | role/table/column policy is applied before returning metadata |
| `search_values` | Find real values for WHERE clauses | reads the controlled `value_index` |
| `validate_sql` | Validate a candidate without execution | single statement, read-only AST, denied functions, table/column policy |
| `query` | Run a validated read query | policy is run again server-side, then PostgreSQL timeout and row cap apply |
| `list_tables` | List business tables | internal metadata tables are hidden; role policy applies |
| `browse_table` | Return one page of a visible table | identifier allowlist, role columns, page-size cap |
| `search_table` | Keyword search over a visible table | parameterized search, role columns, page-size cap |

`query` is intentionally atomic: callers cannot mark arbitrary SQL as
"approved" and execute it through a second unguarded call.

## Policy

`tools/policy.py` uses `sqlglot` rather than SQL string prefixes. It rejects:

- empty or multiple statements;
- DDL and DML, including write operations inside CTEs;
- configured denied functions such as `pg_sleep`;
- tables outside `QUERYAGENT_ALLOWED_TABLES`.

The production implementation is a PostgreSQL MCP server. The migration keeps
this trust boundary and adds a PostgreSQL read-only role, `statement_timeout`,
schema/column policy, row limits, and optional EXPLAIN cost limits. The MCP
server runs as an independent stdio child process owned by the FastAPI backend.
The browser and Agent never receive a DSN. Database roles remain the
authoritative data permission boundary.

SQLite is only a historical migration baseline and is not a supported runtime
path for the Web Demo. Data initialization and fixed-data reset are maintenance
operations owned by the backend; all chat queries, schema access, data
browsing, search, and CSV export go through MCP.

## Lifecycle and Trace

`MCPExecutor` owns one stdio session on a background event loop and exposes
`close()` / context-manager methods. Evaluation closes each executor after a
case, avoiding leaked subprocesses. Production traces should add session id,
turn id, tool call id, policy decision, query hash, row count, and truncation
state.

## Run

During migration, the existing SQLite tests remain a regression baseline. The
PostgreSQL MCP contract will be exercised after the Compose database is
initialized:

```bash
python -m pytest
# later, with Compose running:
python -m scripts.test_mcp_pg
```

The Web Demo runtime does not expose direct SQLite or sandbox executor modes.
