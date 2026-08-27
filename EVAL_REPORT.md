# QueryAgent 评测报告

> Text-to-SQL 数据分析 Agent 的离线评测结果。所有数字来自 `scripts/run_eval.py` / `scripts/run_bird.py` / `scripts/compare_models.py`，可复现（关键对照已用 `--repeats 3` 多次采样）。

## 1. 模型与环境

| 模型 | 类型 | 端点 | 特征 |
|---|---|---|---|
| `deepseek-v4-flash` | 闭源、reasoning | api.deepseek.com | 生成时消耗 reasoning tokens，非确定 |
| `qwen-flash` | 阿里、非 reasoning | Model Studio 兼容端点 | 无 reasoning，更省更快更确定 |
| `MockLLM` | 规则生成器 | 离线 | 确定性，零成本，跑通管线用 |

- 指标：execution accuracy（主）、exact match、平均步数、平均 token、单查询成本、平均延迟。
- 成本按每千 token 单价估算（deepseek-chat 官方价 input $0.27/M / output $1.10/M），**v4-flash 实价待确认**。

## 2. 评测集

- 中文自造 35 条：`mini.jsonl`（12 条）+ `warehouse.jsonl`（23 条，4 领域 17 表）。
- BIRD dev 子集 200 条（英文、11 库、分层抽样）。

## 3. 对照实验结果

### 3.1 自纠正（MockLLM，mini 12 条）

| 基线 | 自纠正 ≤3 轮 | Δ |
|---|---|---|
| 83.3% | **91.7%** | +8.3pp |

`q08` 首次生成编造列名 → 执行错误 → 回灌 → 修正，演示自纠正链路。

### 3.2 真实模型（DeepSeek，mini 12 条，T=0，repeats=3）

execution accuracy **91.7% ± 0.0pp**（稳定）。自纠正 Δ=0，因为剩余失败全是「语义错」（SQL 能执行但结果错），执行层校验抓不到——W4「LLM 自审」已实现该机制（见 3.4）。

### 3.3 schema 感知（DeepSeek，warehouse 23 条，repeats=3）

| 配置 | avg tokens/query | exec accuracy |
|---|---|---|
| 全量注入 17 表 | 2230 | 56.5%（稳定）|
| **embedding 选 top-4 表** | **923** | 50%–67%（噪声大）|

**结论**：schema 选择**可靠地省 58.6% token**（确定）；但**准确率收益在噪声内不可复现**——reasoning 模型 T=0 仍抖动 ±7–15pp（top_k=4 的 baseline 66.7% 与 corrected 50.7% 是同一配置的两次独立采样，0 次纠正）。要确认准确率收益需更多 repeats 或更确定的模型。

### 3.4 语义校验（W4）

规则层（执行错误分类 + 空集启发式）+ LLM 自审（结果回灌自评）。实测自审对「漏 WHERE 过滤」的 SQL 返回 `ok=false` 并给精确修正建议（"需加 WHERE city='北京'"）。

### 3.5 模型对照（warehouse 23 条，top_k=4，repeats=3）

| 模型 | exec accuracy | tokens/query | cost/run | latency |
|---|---|---|---|---|
| deepseek-v4-flash | 59.4% | 944 | $0.011 | 3192 ms |
| **qwen-flash** | **76.8%** | **691** | **$0.005** | **1110 ms** |

**结论**：非推理的 qwen-flash 在中文任务上**反超推理型 deepseek-v4-flash 17.4pp**，同时成本减半、快 3 倍。reasoning tokens 在简单查询上多为浪费。

### 3.6 BIRD dev 子集（DeepSeek，200 条）

execution accuracy **56.0%**（零样本、全量 schema 注入）。对照：BIRD 排行榜 SOTA ~80%（重度 schema-linking 专用系统）。

## 4. 关键发现

1. **schema 选择的 token 节省可靠（-58.6%）**，准确率收益需更强证据。
2. **reasoning 模型 ≠ 更好**：qwen-flash 更省、更快、更准、更确定。
3. **reasoning 模型 T=0 仍非确定**（±7–15pp），评测必须多次采样报均值±方差。
4. **自纠正在语义错误上需 LLM 自审补齐**（机制已实现，聚合收益待复测）。

## 5. 已知限制 / 待办

- reasoning 模型噪声大，关键准确率结论建议 repeats≥5 或换更确定模型复测。
- 成本为估算价，接入真实计费后校准。
- Docker 沙箱已实测通过（容器只读挂载 + SQL 守卫双层防护；OS 层写入返回 `attempt to write a readonly database`）。
