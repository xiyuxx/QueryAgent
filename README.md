# QueryAgent

QueryAgent 是一个本地运行的中文数据分析 Agent Demo。浏览器只访问 React/Vite 前端，查询请求由 FastAPI 编排 Agent，并通过独立 stdio MCP Server 访问 PostgreSQL + pgvector。

## 一键启动

环境要求：Docker Desktop（Windows、macOS、Linux 均可）和至少一个模型 Provider 的 API Key。

```bash
cp .env.example .env
# 编辑 .env，填写 DEEPSEEK_API_KEY、QWEN_API_KEY 或 OPENAI_API_KEY

docker compose up --build
```

打开 <http://localhost:5173>。停止服务：

```bash
docker compose down
```

如需同时删除演示数据库卷：

```bash
docker compose down -v
```

首次初始化会创建确定性的 PostgreSQL 演示数据、schema metadata、value index 和 pgvector 表向量。embedding 下载失败会让 `db-init` 失败，修复网络后重新执行 `docker compose up --build` 即可重试。

## 页面

- **查询**：聊天式 Text-to-SQL，支持 DeepSeek、Qwen、OpenAI、角色切换、本地多会话、SSE 阶段流、SQL、结果表、总结和指标。
- **数据**：按角色浏览业务表，每页最多 50 行，支持整表关键词搜索和 CSV 导出。
- **评测**：选择 `mini` 或 `warehouse` 数据集启动后台评测。实时评测固定使用 `admin` 角色，Provider 使用当前选择；后端重启后任务和结果不保留。

## 安全边界

- API Key 只存在后端 `.env`，不会进入前端构建产物或 Git。
- Agent 不直连数据库；查询、schema、浏览、搜索和 CSV 均经过 PostgreSQL MCP stdio 子进程。
- MCP 子进程只接收数据库级只读账号 DSN，不接收管理员 DSN 或模型凭证。
- SQL 只允许 PostgreSQL 只读语句，并执行表级角色权限、敏感列规则、statement timeout 和行数上限。
- 敏感字段字段名可见；未授权值显示 `******`。明确读取敏感列会被 MCP 拒绝。
- 查询过程中切换角色不会改变当前请求使用的角色；下一次请求才使用新角色。

## 本地开发

后端：

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev,postgres,web]'
.venv/bin/pytest -q
.venv/bin/uvicorn queryagent.api.app:app --reload --port 8000
```

前端：

```bash
cd frontend
npm ci
npm run typecheck
npm run build
npm run dev
```

开发模式下 Vite 将 `/api` 代理到 `http://localhost:8000`。正式演示仍推荐使用 Docker Compose，因为它会统一启动 PostgreSQL、db-init、FastAPI、MCP 和 Nginx。

## 项目结构

```text
src/queryagent/agent       AgentLoop 与自纠正
src/queryagent/api         FastAPI、SSE、数据和评测 API
src/queryagent/database    PostgreSQL 初始化和确定性种子数据
src/queryagent/llm         Provider、OpenAI-compatible client、MockLLM
src/queryagent/tools       MCP client/server、PostgreSQL 策略和 RBAC
frontend                   React/Vite 查询、数据、评测界面
tests                      离线单元测试和 API 替身测试
```

英文说明见 [README.en.md](README.en.md)。设计基线见 [docs/WEB_DEMO_DESIGN.md](docs/WEB_DEMO_DESIGN.md)。
