# MCP Execution Boundary

## Scope

MCP is the only execution boundary between the agent and a data source. The
agent may propose SQL, but it cannot access a DSN, a database path, resource
limits, or authorization settings.

## Tools

| Tool | Purpose | Server enforcement |
|---|---|---|
| `get_schema` | Return visible DDL | table allowlist is applied before returning metadata |
| `validate_sql` | Validate a candidate without execution | single statement, read-only AST, denied functions, table allowlist |
| `query` | Run a validated read query | policy is run again server-side, then sandbox timeout and row cap apply |

`query` is intentionally atomic: callers cannot mark arbitrary SQL as
"approved" and execute it through a second unguarded call.

## Policy

`tools/policy.py` uses `sqlglot` rather than SQL string prefixes. It rejects:

- empty or multiple statements;
- DDL and DML, including write operations inside CTEs;
- configured denied functions such as `pg_sleep`;
- tables outside `QUERYAGENT_ALLOWED_TABLES`.

The current implementation is a SQLite MCP server for deterministic local
evaluation. The next backend should keep the same tool contracts and add a
PostgreSQL read-only role, `statement_timeout`, schema/column policy, and
EXPLAIN cost limits. Docker remains a local isolation backend; database roles
remain the authoritative data permission boundary.

## Lifecycle and Trace

`MCPExecutor` owns one stdio session on a background event loop and exposes
`close()` / context-manager methods. Evaluation closes each executor after a
case, avoiding leaked subprocesses. Production traces should add session id,
turn id, tool call id, policy decision, query hash, row count, and truncation
state.

## Run

```bash
python -m scripts.test_mcp
python -m scripts.run_eval --llm mock --executor mcp
python -m scripts.run_eval --llm qwen --executor mcp
```

For local debugging only, the direct paths remain available:

```bash
python -m scripts.run_eval --executor sqlite
python -m scripts.run_eval --executor sandbox
```
