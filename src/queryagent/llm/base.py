"""LLM 客户端统一抽象：Structured Output（JSON Schema + Pydantic）。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Type

from pydantic import BaseModel


class SQLCandidate(BaseModel):
    """Structured Output 卡死的字段：SQL 与一句解释。"""

    sql: str
    explanation: str = ""


class AuditResult(BaseModel):
    """结果自审输出：执行结果是否正确回答了用户问题。"""

    ok: bool
    reason: str = ""


class ValueCandidates(BaseModel):
    """值检索输出：问题中可能对应数据库实际取值的关键词。"""

    values: list[str] = []


class IntentResult(BaseModel):
    """意图分类输出。"""

    intent: str = "query"  # query / metadata / chat


@dataclass
class Usage:
    """单次 LLM 调用的消耗统计。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class LLMResponse:
    content: str
    usage: Usage = field(default_factory=Usage)
    latency_ms: float = 0.0
    parsed: Optional[BaseModel] = None


SQL_SYSTEM_PROMPT = (
    "你是一个 Text-to-SQL 助手。根据给定的数据库 schema 和用户问题，"
    "生成一条可执行的 SQLite SQL 查询，并用一句中文说明思路。"
    "输出一个 JSON 对象，包含 sql 与 explanation 两个字段。"
)

AUDIT_SYSTEM_PROMPT = (
    "你是一个 SQL 结果校验器。判断给定 SQL 的执行结果是否正确、完整地回答了用户问题。"
    "输出 JSON 对象，含 ok（布尔）与 reason（字符串）字段；结果有误时 ok=false 并说明原因。"
)

VALUE_EXTRACT_SYSTEM = (
    "你负责从用户问题中提取可能对应数据库实际取值的关键词（地名、人名、状态、品类、"
    "机构名等），这些值将用于 SQL 的 WHERE 条件。输出 JSON 对象，含 values 字符串数组。"
)


INTENT_SYSTEM = (
    "你是意图分类器。判断用户输入属于 query（要查数据）、metadata（问表/列结构）、chat（闲聊/问候）。"
    "输出 JSON 对象，含 intent 字段，取值只能是 query/metadata/chat。"
)

STRATEGY_HINTS = {
    "divide": "采用分治策略：先把复杂问题拆解成子问题，逐步生成子查询，再合并成最终 SQL。",
    "plan": "按照数据库执行计划推理：先定位相关表，再考虑过滤、连接，最后确定输出列。",
}


def build_intent_prompt(question: str) -> str:
    return f"用户输入：{question}\n\n请判断其意图（query/metadata/chat）。"


def build_sql_prompt(question: str, schema_ddl: str, feedback: list[str]) -> str:
    parts = ["数据库 schema（DDL）：\n" + schema_ddl]
    parts.append("用户问题：" + question)
    if feedback:
        parts.append("上一次生成失败，请根据以下错误信息修正：")
        parts.extend("- " + f for f in feedback)
    return "\n\n".join(parts)


def build_audit_prompt(question: str, sql: str, result_preview: str) -> str:
    return (
        f"用户问题：{question}\n"
        f"执行的 SQL：{sql}\n"
        f"执行结果（前若干行）：\n{result_preview}\n\n"
        "该结果是否正确回答了用户问题？若明显错误（列不对、值异常、空结果但问题期望有数据、"
        "聚合方式错误等），输出 ok=false 并给出具体原因；否则 ok=true。"
    )


def build_value_prompt(question: str, schema_context: str = "") -> str:
    parts = ["用户问题：" + question]
    if schema_context:
        parts.append("相关表结构：\n" + schema_context)
    parts.append("提取问题中所有可能作为 WHERE 条件取值的关键词。")
    return "\n\n".join(parts)


class LLMClient(ABC):
    """统一 LLM 接口。闭源 API 与 Qwen 均实现此接口。"""

    @abstractmethod
    def generate(
        self,
        prompt: str,
        *,
        system: str = "",
        response_model: Optional[Type[BaseModel]] = None,
    ) -> LLMResponse:
        """生成结构化输出。response_model 提供 JSON Schema 约束。"""

    def generate_sql(
        self, question: str, schema_ddl: str, feedback: list[str], strategy: str = "standard"
    ) -> LLMResponse:
        """生成 SQL 候选。strategy 控制推理方式（standard/divide/plan）。"""
        prompt = build_sql_prompt(question, schema_ddl, feedback)
        if strategy and strategy != "standard":
            prompt += "\n\n推理策略：" + STRATEGY_HINTS.get(strategy, "")
        resp = self.generate(
            prompt, system=SQL_SYSTEM_PROMPT, response_model=SQLCandidate
        )
        if resp.parsed is None and resp.content:
            resp.parsed = SQLCandidate.model_validate_json(resp.content)
        return resp

    def audit(self, question: str, sql: str, result_preview: str) -> LLMResponse:
        """结果自审：判断执行结果是否正确回答了用户问题。"""
        prompt = build_audit_prompt(question, sql, result_preview)
        resp = self.generate(
            prompt, system=AUDIT_SYSTEM_PROMPT, response_model=AuditResult
        )
        if resp.parsed is None and resp.content:
            resp.parsed = AuditResult.model_validate_json(resp.content)
        return resp

    def extract_values(self, question: str, schema_context: str = "") -> list[str]:
        """值检索：提取问题中可能作为 WHERE 条件取值的候选关键词。"""
        prompt = build_value_prompt(question, schema_context)
        resp = self.generate(
            prompt, system=VALUE_EXTRACT_SYSTEM, response_model=ValueCandidates
        )
        if resp.parsed is not None:
            return resp.parsed.values
        return []

    def classify_intent(self, question: str) -> str:
        """意图分类：query（查数据）/ metadata（问结构）/ chat（闲聊）。"""
        resp = self.generate(
            build_intent_prompt(question), system=INTENT_SYSTEM, response_model=IntentResult
        )
        if resp.parsed is not None:
            return resp.parsed.intent
        return "query"
