# QueryAgent

QueryAgent is a local Chinese data-analysis Agent demo. The browser talks only to the React/Vite frontend. FastAPI orchestrates the Agent and reaches PostgreSQL + pgvector through a separate stdio MCP server.

## Quick Start

Requirements: Docker Desktop on Windows, macOS, or Linux, plus an API key for at least one model provider.

```bash
cp .env.example .env
# Edit .env and fill DEEPSEEK_API_KEY, QWEN_API_KEY, or OPENAI_API_KEY

docker compose up --build
```

Open <http://localhost:5173>. Stop the stack with `docker compose down`. To remove the demo database volume as well, use `docker compose down -v`.

The first initialization creates deterministic PostgreSQL demo data, schema metadata, the value index, and table vectors. A failed embedding download makes `db-init` fail; fix the network and run Compose again to retry.

## Pages

- **Query**: chat-based Text-to-SQL with DeepSeek, Qwen, and OpenAI, role switching, local sessions, SSE stages, SQL, result tables, summaries, and metrics.
- **Data**: role-aware table browsing, pages of up to 50 rows, full-table keyword search, and CSV export.
- **Evaluation**: run the `mini` or `warehouse` dataset as a background task. Live evaluation always uses the `admin` role and the currently selected provider. Tasks and results are lost when the backend restarts.

## Security Boundary

- API keys stay in the backend `.env` and never enter frontend assets or Git.
- The Agent does not connect to the database directly. Query, schema, browsing, search, and CSV operations go through the PostgreSQL MCP stdio child process.
- The MCP child receives only a database-level read-only DSN, never the admin DSN or model credentials.
- SQL is restricted to read-only PostgreSQL statements and is checked for table permissions, sensitive columns, statement timeout, and row limits.
- Sensitive field names remain visible. Unauthorized values are returned as `******`; explicit sensitive-column access is rejected by MCP.
- Changing the role during a query cannot change the role of that request; the next request uses the new role.

## Local Development

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev,postgres,web]'
.venv/bin/pytest -q
.venv/bin/uvicorn queryagent.api.app:app --reload --port 8000
```

In another terminal:

```bash
cd frontend
npm ci
npm run typecheck
npm run build
npm run dev
```

Vite proxies `/api` to `http://localhost:8000` in development. Docker Compose is recommended for the complete demo because it starts PostgreSQL, initialization, FastAPI, MCP, and Nginx together.

See [README.md](README.md) for the Chinese guide and [docs/WEB_DEMO_DESIGN.md](docs/WEB_DEMO_DESIGN.md) for the design baseline.
