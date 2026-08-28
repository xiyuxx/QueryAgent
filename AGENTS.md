# QueryAgent — 项目指引（给后续 AI 协作者的操作手册）

> 本文件是项目级约定与当前状态基线。后续任何 AI 接手，先读本文件 + `DESIGN.md`，再动手。
> 目标场景：字节/阿里/智谱 Agent 开发岗秋招简历项目。

---

## 一、项目一句话

从零实现一个 **Text-to-SQL 数据分析 Agent**，核心卖点不是"能查库"，而是：
**自建评测体系 + 可靠性链路，用可复现的数据证明成功率、成本、延迟的真实改进。**

## 二、目标（最终交付物）

1. 一个能处理多轮查询、带 schema 感知与自纠正能力的 Text-to-SQL agent。
2. 一套离线评测 harness：数据集 + 指标 + tracing，能定位"哪一步最常失败"。
3. 一组可写进简历的对照数字：成功率、单查询成本、平均延迟、平均步数。
4. 一份评测报告（含 Qwen 与闭源模型的对照）。

## 三、技术栈（已锁定，别推翻）

| 项 | 选择 | 说明 |
|---|---|---|
| 语言 | Python 3.11+ 主体 | 可选 Go 写评测并发调度层（后续） |
| 模型 | 闭源 API 为主 + Qwen 开源对照 | 投阿里时 Qwen 对照是加分项 |
| Agent 抽象 | **自研，禁用 LangChain/LlamaIndex 封装 loop** | 核心逻辑自己写，这是"懂底层"的证据 |
| 工具协议 | **MCP**（把数据库查询封装成 MCP tool） | 面试考点，不能硬编码函数 |
| Structured Output | JSON Schema + Pydantic | 卡死字段，下游可解析 |
| 数据库 | **PostgreSQL + pgvector** | 正式 Demo 唯一数据源；不保留 SQLite 运行路径 |
| 执行隔离 | PostgreSQL 只读角色 + statement_timeout + 行数上限 | MCP 服务端是唯一执行边界 |
| Tracing | 自建轻量 span（后续可接 OpenTelemetry） | 关键是"能定位失败步" |

## 四、目录结构（规划）

```
QueryAgent/
├── AGENTS.md              # 本文件
├── DESIGN.md              # 核心设计与约束
├── docs/                  # Web Demo 设计与验收标准
├── docker-compose.yml     # PostgreSQL、FastAPI、前端一键启动编排
├── docker/                # 后端/前端镜像与 Nginx 配置
├── frontend/              # React + Vite + TypeScript 展示界面
├── pyproject.toml         # Python 依赖与配置
├── src/queryagent/
│   ├── agent/             # agent loop 主控
│   ├── api/               # FastAPI HTTP/SSE 接口（后续阶段）
│   ├── llm/               # 模型客户端（DeepSeek/Qwen/OpenAI）
│   ├── tools/             # PostgreSQL MCP 工具与客户端
│   ├── schema/            # schema 感知/选择
│   ├── reliability/       # 结果校验 + 自纠正
│   ├── eval/              # 评测 harness
│   └── observability/     # tracing / 指标
├── eval_sets/             # 评测查询集
├── data/                  # 不提交运行时数据库文件
└── scripts/               # 初始化、评测与维护脚本
```

## 五、核心约定（工作规则，MUST）

1. **不用框架封装 agent loop**：`plan → tool_call → observe → 判断 → 循环` 自己写。
2. **评测是最高优先级**：任何可靠性/优化改动，必须先跑评测出数字（对照实验），禁止"改了感觉更好"。
3. **每个模块独立可测**：模块间通过明确接口解耦，单模块能单独 import 测试。
4. **可靠性改动要有对照**：如"加 self-correction 后成功率 +X%"，要能复现这个 X。
5. **复用已有模式，不引入第二套惯例**：新增代码遵循现有目录与命名。
6. **验证 = 跑通评测 + 复现数字**，不是"能 import"。

## 六、关键设计决策（已定，勿重设计）

- 主攻 **Text-to-SQL** 场景（不是 Coding Agent / GUI Agent）。
- 可靠性分三层：SQL 语法校验 → 结果合理性校验 → 幻觉检测 + 自纠正重试。
- schema 感知用"只注入相关表 + 少量样本"，控制 token、防上下文腐化。
- 评测指标：execution accuracy、exact match、平均步数、token 消耗、成本、延迟。

## 七、当前状态

- [x] 原有 Text-to-SQL Agent、评测 harness、SQLite/MCP 原型和 RBAC 测试已存在
- [x] Git 仓库已初始化，`main` 与 GitHub 远端同步
- [x] 本地 Web Demo 产品需求已确认，见 `docs/WEB_DEMO_DESIGN.md`
- [ ] Phase 0：工程骨架与 Compose 基线
- [ ] Phase 1：PostgreSQL + pgvector 模拟生产数据库
- [ ] Phase 2：PostgreSQL MCP 与数据浏览工具
- [ ] Phase 3：Agent/Provider/FastAPI/SSE
- [ ] Phase 4：React 查询工作台
- [ ] Phase 5：数据浏览与重置
- [ ] Phase 6：评测控制台
- [ ] Phase 7：一键启动、README 与最终验收

## 八、接手与实施顺序

1. 先读本文件、`DESIGN.md` 和 `docs/WEB_DEMO_DESIGN.md`。
2. 正式运行路径统一为 PostgreSQL + pgvector；SQLite 兼容代码仅在迁移阶段保留，Phase 1 后删除。
3. 聊天、数据浏览、搜索和导出必须经过 PostgreSQL MCP；初始化/重置是唯一允许后端直连数据库的维护操作。
4. 每个阶段先跑自动化测试，测试通过后独立提交、推送并合并到远端 `main`。
5. 任何可靠性、权限或性能改动都必须有可复现验证，不接受只凭主观判断的改动。

## 九、验证方式

- 当前基线测试：`python -m pytest`。
- 迁移后评测：通过 Docker/后端统一使用 PostgreSQL，输出成功率/成本/延迟表格。
- 前端阶段：运行 TypeScript 检查、组件测试和生产构建。
- Compose 阶段：运行 `docker compose config`、镜像构建和端到端冒烟测试。
- 每个里程碑都要有可复现的数字，写进评测报告或阶段产物。
