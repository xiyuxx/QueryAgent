# QueryAgent 本地 Web Demo 技术设计

> 状态：Phase 0 设计基线（2026-08-28）
>
> 本文记录本地展示版的已确认需求、系统边界和分阶段验收标准。后续实现以本文为产品基线；如果实现与本文冲突，需要先更新设计并说明原因。

## 1. 产品目标

QueryAgent 提供一个面向桌面浏览器的本地展示系统。用户通过一个地址进入系统，能够：

1. 用聊天方式提出中文数据问题；
2. 看到真实的 Text-to-SQL、MCP 执行、校验和自纠正过程；
3. 在不同角色下即时观察表、字段和查询权限差异；
4. 浏览模拟生产数据库中的业务数据；
5. 查看已有评测快照，并按选择的模型运行实时评测。

正式运行模式统一使用 **PostgreSQL + pgvector**。不保留 SQLite 作为正式 Demo、降级或备用运行路径。

## 2. 用户运行方式

最终用户只需要安装 Docker Desktop，复制并填写环境变量模板：

```bash
cp .env.example .env
# 编辑 .env，至少填写一个真实模型 API Key

docker compose up --build
```

用户只访问：

```text
http://localhost:5173
```

前端由 Nginx 提供，`/api/*` 反向代理到 FastAPI。PostgreSQL、FastAPI、MCP Server 和前端均由 Compose 管理。

## 3. 系统拓扑

```text
Browser :5173
    │
    ▼
Nginx / React
    │ /api/*
    ▼
FastAPI :8000
    ├── AgentLoop
    ├── Provider Registry
    ├── Evaluation task manager
    └── MCPExecutor
          │ stdio（独立子进程）
          ▼
    PostgreSQL MCP Server
          │
          ▼
    PostgreSQL + pgvector :5432
```

### 3.1 信任边界

- 浏览器不会收到 API Key、DSN 或数据库密码；
- Agent 不直接连接 PostgreSQL；
- 聊天、schema、值检索、数据浏览、搜索和 CSV 导出都经过 MCP；
- MCP Server 在服务端再次执行 SQL、表、列和角色权限检查；
- 数据重置和初始化属于维护操作，由后端直接调用初始化逻辑，不经过查询 MCP；
- `schema_metadata`、`value_index`、`table_embeddings` 是内部技术表，不展示在数据浏览页。

## 4. 页面

### 4.1 查询工作台 `/`

- 聊天式查询为默认首页；
- 支持数据查询、schema/表结构问答和普通问候；
- 数据查询通过 SSE 实时展示识别意图、schema、SQL、MCP、校验、纠正和总结阶段；
- 中文总结由当前真实模型额外生成，失败时用固定模板降级；
- 同时展示中文总结、SQL、结果表、自动图表和执行指标；
- 图表由前端根据结果形状判断，不额外调用模型；
- 查询请求显式携带当前角色和 Provider；
- 当前请求执行期间禁用发送，不并行执行查询；
- 查询中切换角色时，当前请求仍使用发送时的角色，并提示用户；
- Provider 网络故障时可按规则临时切换备用 Provider，消息中标出实际使用的模型。

### 4.2 数据浏览 `/data`

- 跟随全局当前角色；
- 初始只显示数据库概览和业务表列表，不自动选表；
- 点击表后显示字段、分页数据和全表关键词搜索；
- 默认每页 50 行；
- 支持导出当前分页内容 CSV；
- 相关接口全部经过 MCP；
- 敏感字段名称可见，未授权值显示为 `******`；明确查询未授权敏感字段时由 MCP 直接拒绝；
- 所有角色都可以通过二次确认执行“重置为初始数据”；重置后清空浏览器本地会话；
- 聊天或评测正在执行时禁用重置按钮。

### 4.3 评测控制台 `/console`

- 默认展示仓库中的历史评测快照；
- 实时评测前选择 `mini` 或 `warehouse`；
- 使用主界面当前 Provider，固定使用 `admin` 角色；
- 评测在后端后台任务中运行，离开页面不影响任务；
- 后端重启后运行中任务和已完成结果均丢失；
- 展示总体指标、单条用例、最终 SQL、gold SQL、结果一致性、纠正次数和阶段摘要；
- 不展示完整 Prompt，不提供评测结果下载，不自动生成历史快照对比。

### 4.4 全局状态

顶部只显示项目名称 `QueryAgent`，并在所有页面提供：

- 当前 Provider 切换；
- 当前角色切换；
- 系统准备状态；
- 页面导航。

Provider 和角色保存到 `localStorage`。聊天支持多个本地会话，刷新后保留；发送给模型时只携带当前角色最近 5 轮上下文。

## 5. Provider

首版支持 DeepSeek、Qwen 和 OpenAI。每个 Provider 只配置一个模型，前端只显示已填写 API Key 的 Provider。

故障转移规则：

1. 当前选中的 Provider 优先；
2. 其后按 `DeepSeek → Qwen → OpenAI` 过滤已配置 Provider；
3. 仅网络错误、超时、服务端 5xx 等可恢复错误触发切换；
4. API Key、参数、结构化输出解析、权限和 SQL 逻辑错误不切换；
5. 备用 Provider 只对当前请求生效，不改变用户的选择。

## 6. 模拟生产数据库

数据库由初始化脚本使用固定种子生成，不把大数据库文件提交到 Git。业务表分为四个领域：

- 电商：`customers`、`products`、`orders`、`order_items`、`reviews`；
- 金融：`accounts`、`transactions`、`loans`、`credit_cards`；
- 人力：`employees`、`departments`、`salaries`、`attendance`；
- 物流：`shipments`、`warehouses`、`carriers`、`delivery_routes`。

加入合成敏感字段：

- `customers.phone`；
- `customers.email`；
- `employees.national_id`。

数据规模控制在适合下载和本地 Docker 启动的范围：维表为数百到数千行，事实表为数千到数万行。每次重置恢复完全一致的数据。

初始化同时建立：

- PostgreSQL 业务表；
- `schema_metadata`；
- `value_index`；
- `table_embeddings`。

首次初始化自动下载 `BAAI/bge-small-zh-v1.5` embedding 模型，并使用 Docker volume 缓存。下载失败时不降级为 BM25，状态页提供重试。

## 7. MCP 工具边界

目标工具契约：

| 工具 | 用途 |
|---|---|
| `get_schema` | 返回当前角色可见 schema，并支持相关表检索 |
| `search_values` | 从 `value_index` 检索真实业务值 |
| `validate_sql` | 在 PostgreSQL dialect 下做只读、单语句和权限校验 |
| `query` | 原子地校验并执行查询，应用超时、行数和脱敏策略 |
| `list_tables` | 返回当前角色可见的业务表 |
| `browse_table` | 返回角色过滤后的分页数据 |
| `search_table` | 对指定可见表执行关键词搜索并分页 |

MCP 使用 PostgreSQL 只读数据库角色、`statement_timeout`、行数上限和 `sqlglot` AST 策略形成多层防护。权限拒绝不会进入 Agent 自纠正循环。

## 8. 阶段与验收

### Phase 0：设计与工程基线

- 更新设计文档和 `.env.example`；
- 建立前端、后端、Compose 和 PostgreSQL 基础目录；
- 验证当前测试不回归。

### Phase 1：PostgreSQL、pgvector 和固定种子数据

- 初始化业务表、默认数据和技术索引；
- 验证幂等、重置一致性、扩展和 embedding 缓存；
- 当前生成器固定为 17 张业务表、约 3.3 万行合成数据；
- 真实 PostgreSQL/pgvector 集成测试需要 Docker 或本机 PostgreSQL 环境，当前源代码环境完成了静态和单元验证。

### Phase 2：PostgreSQL MCP 与 RBAC

- MCP Server 改为 PostgreSQL stdio 子进程；
- 实现查询、schema、值检索和数据浏览工具；
- 验证只读策略、分页、搜索、脱敏和审计。

### Phase 3：Agent、Provider 和 FastAPI

- 加入角色、历史、SSE 事件、总结降级和 Provider 故障转移；
- 验证权限拒绝、上下文过滤和替身模型调用。

### Phase 4：React 查询工作台

- 实现全局布局、聊天、多会话、SSE、结果、图表和 CSV；
- 验证 TypeScript、组件和浏览器状态。

### Phase 5：数据浏览与重置

- 实现概览、表、分页、搜索、脱敏、导出和重置；
- 验证角色联动和重置期间的并发保护。

### Phase 6：评测控制台

- 实现快照、后台实时评测、进度和明细；
- 验证固定 `admin` 角色和任务生命周期。

### Phase 7：Compose、启动状态和文档

- 完成一键启动、embedding 重试、Nginx SSE、中文/英文 README；
- 完成跨平台 Docker Desktop 验收。

每一阶段都必须：

1. 先运行对应自动化测试；
2. 测试通过后提交到独立 feature 分支；
3. 推送分支并合并到远端 `main`；
4. 汇报测试、commit 和远端状态后再进入下一阶段。
