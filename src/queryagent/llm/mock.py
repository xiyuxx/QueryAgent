"""确定性规则 SQL 生成器（离线评测用）。

无需 API key，保证评测数字可复现；实现 LLMClient 契约，成本/延迟照常统计。
它是弱生成器：只覆盖有限意图，用于跑通评测管线；真实模型（闭源/Qwen）经
OpenAICompatClient 替换后，agent loop 与 harness 不变。

刻意设计：首次生成把“名字/姓名”解析成不存在的列名 customer_name，
触发执行错误 → 自纠正 → 按规范列名 name 重新生成，用于演示自纠正链路。
"""
from __future__ import annotations

import re
import time

from .base import AuditResult, LLMClient, LLMResponse, SQLCandidate, Usage

_CITIES = ("北京", "上海", "深圳", "广州", "杭州")

# 关键词 → 规范列名（纠正后使用）
_CANONICAL = {
    "名字": "name",
    "姓名": "name",
    "城市": "city",
    "价格": "price",
    "分类": "category",
    "类别": "category",
    "数量": "quantity",
    "日期": "order_date",
}

# 首次生成的“朴素猜测”列名（刻意错误，触发自纠正）
_NAIVE = {"名字": "customer_name", "姓名": "customer_name"}


def _strip_punct(text: str) -> str:
    return re.sub(r"[，。！？；：、\s“”‘’（）()]", "", text)


def _extract_city(q: str) -> str | None:
    for c in _CITIES:
        if c in q:
            return c
    return None


def _extract_number(q: str) -> int | None:
    m = re.search(r"(\d+)", q)
    return int(m.group(1)) if m else None


def _tokens_for(text: str) -> int:
    # 粗略估算：中文约 1 字/token，这里按 3 字符/token 估
    return max(1, len(text) // 3)


class MockLLM(LLMClient):
    """规则 SQL 生成器，输出符合 LLMClient 契约，便于评测统计成本/延迟。"""

    def __init__(self, *, price_per_1k_tokens: float = 0.001) -> None:
        self.price_per_1k_tokens = price_per_1k_tokens

    def generate(self, prompt, *, system="", response_model=None):
        # 该客户端走 generate_sql 覆盖路径；generate 仅满足接口完整性。
        return LLMResponse(content="", usage=Usage())

    def generate_sql(self, question, schema_ddl, feedback, strategy="standard", history=None) -> LLMResponse:
        t0 = time.perf_counter()
        correcting = bool(feedback)
        sql, explanation = self._rule_generate(question, correcting)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        prompt_tokens = _tokens_for(question + schema_ddl)
        completion_tokens = _tokens_for(sql + explanation)
        usage = Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=(prompt_tokens + completion_tokens) / 1000.0 * self.price_per_1k_tokens,
        )
        return LLMResponse(
            content=sql,
            usage=usage,
            latency_ms=latency_ms,
            parsed=SQLCandidate(sql=sql, explanation=explanation),
        )

    def answer_text(self, question, *, context="", history=None) -> LLMResponse:
        return LLMResponse(
            content="",
            usage=Usage(),
            parsed=__import__("queryagent.llm.base", fromlist=["TextAnswer"]).TextAnswer(
                answer="这是一个离线 Mock 回答。"
            ),
        )

    def summarize_result(self, question, sql, columns, rows) -> LLMResponse:
        if not rows:
            answer = "查询完成，结果为空。"
        elif len(rows) == 1 and len(rows[0]) == 1:
            answer = f"查询完成，结果为 {rows[0][0]}。"
        else:
            answer = f"查询完成，共返回 {len(rows)} 行。"
        return LLMResponse(content="", usage=Usage(), parsed=__import__("queryagent.llm.base", fromlist=["TextAnswer"]).TextAnswer(answer=answer))

    def audit(self, question, sql, result_preview) -> LLMResponse:
        # 规则生成器不做语义自审（无真实语义判断能力），恒通过。
        return LLMResponse(content="", usage=Usage(), parsed=AuditResult(ok=True, reason=""))

    def extract_values(self, question, schema_context="") -> list[str]:
        # 启发式：按标点切分，取非空词块作为候选值
        return [w for w in re.split(r"[，。！？；：、\s]+", question) if w]

    def classify_intent(self, question) -> str:
        if any(w in question for w in ("你好", "谢谢", "嗨", "再见")):
            return "chat"
        if any(w in question for w in ("表", "列", "字段", "结构", "schema")):
            return "metadata"
        return "query"

    def _resolve_col(self, keyword: str, correcting: bool) -> str:
        if keyword in _NAIVE and not correcting:
            return _NAIVE[keyword]
        return _CANONICAL.get(keyword, keyword)

    def _rule_generate(self, question: str, correcting: bool) -> tuple[str, str]:
        q = _strip_punct(question)
        city = _extract_city(q)
        n = _extract_number(q)

        # 1) 总销售额：orders × products
        if any(k in q for k in ("销售额", "总金额", "营业额", "销售总额")):
            sql = (
                "SELECT SUM(o.quantity * p.price) "
                "FROM orders o JOIN products p ON o.product_id = p.product_id"
            )
            return sql, "orders 与 products 连接后对 quantity*price 求和"

        # 2) 某城市的客户买了哪些产品：三表 join
        if "买" in q and "客户" in q and "产品" in q and city:
            sql = (
                "SELECT DISTINCT p.name "
                "FROM orders o "
                "JOIN customers c ON o.customer_id = c.customer_id "
                "JOIN products p ON o.product_id = p.product_id "
                f"WHERE c.city = '{city}'"
            )
            return sql, "按城市过滤的三表 join，去重返回产品名"

        # 3) 每个/各 … 的分组聚合
        if "每个" in q or "各" in q:
            if "平均" in q:
                group_col = "category" if ("分类" in q or "类别" in q) else "city"
                sql = f"SELECT {group_col}, AVG(price) FROM products GROUP BY {group_col}"
                return sql, f"按 {group_col} 分组的平均价格"
            if "城市" in q:
                return "SELECT city, COUNT(*) FROM customers GROUP BY city", "按城市分组计数"
            if "分类" in q or "类别" in q:
                return "SELECT category, COUNT(*) FROM products GROUP BY category", "按分类分组计数"

        # 4) 最高/最贵 的产品名
        if ("最高" in q or "最贵" in q or "最大" in q) and any(
            k in q for k in ("产品", "名字", "什么", "哪个")
        ):
            return "SELECT name FROM products ORDER BY price DESC LIMIT 1", "按价格降序取第一名"

        # 5) 名字列表（首次生成有列名 bug）
        if "名字" in q or "姓名" in q:
            table = "customers" if "客户" in q else "products"
            col = self._resolve_col("名字", correcting)
            return f"SELECT {col} FROM {table} ORDER BY {col}", f"列出 {table} 的名字"

        # 6) 城市列表
        if "城市" in q:
            return "SELECT DISTINCT city FROM customers ORDER BY city", "去重列出城市"

        # 7) 某城市的计数
        if city and any(k in q for k in ("多少", "数量", "个数")):
            return f"SELECT COUNT(*) FROM customers WHERE city = '{city}'", "按城市过滤计数"

        # 8) 数量大于 N 的订单
        if "大于" in q and n is not None:
            return f"SELECT * FROM orders WHERE quantity > {n}", "数量大于阈值的订单"

        # 9) 某分类的产品
        for cat in ("电子", "服饰", "家居"):
            if cat in q and "产品" in q:
                return f"SELECT * FROM products WHERE category = '{cat}'", f"按分类 {cat} 过滤产品"

        # 10) 某年某月的订单数
        m = re.search(r"(\d{4})年(\d{1,2})月", q)
        if m and "订单" in q:
            yy, mm = m.group(1), m.group(2).zfill(2)
            return f"SELECT COUNT(*) FROM orders WHERE order_date LIKE '{yy}-{mm}%'", "按月份过滤计数"

        # fallback：未识别意图 → 返回客户表前 5 行（真实模型会覆盖）
        return "SELECT * FROM customers LIMIT 5", "未识别意图，回退到客户表"
