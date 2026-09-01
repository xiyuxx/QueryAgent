from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from queryagent.agent.loop import AgentLoop
from queryagent.api.app import AppServices, create_app
from queryagent.llm import (
    LLMClient,
    LLMResponse,
    ProviderConfig,
    ProviderError,
    ProviderRegistry,
    RoutedLLMClient,
    SQLCandidate,
    TextAnswer,
    Usage,
)
from queryagent.reliability.validator import ResultValidator
from queryagent.tools.access import load_access_config
from queryagent.tools.db import QueryResult


class FakeSchema:
    last_tables = ["customers"]

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def context_for(self, question: str, *, role: str = "readonly") -> str:
        self.calls.append((question, role))
        return "CREATE TABLE customers (id integer, name text);"


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def execute(self, sql: str, role: str | None = None) -> QueryResult:
        self.calls.append((sql, role))
        return QueryResult(columns=["count"], rows=[(3,)])

    def close(self) -> None:
        pass


class ScriptedLLM(LLMClient):
    def __init__(
        self,
        *,
        intent: str = "query",
        summary_error: bool = False,
        answer: str = "模型回答",
    ) -> None:
        self.intent = intent
        self.summary_error = summary_error
        self.answer = answer
        self.sql_histories: list[list[dict]] = []
        self.text_histories: list[list[dict]] = []

    def generate(self, prompt, *, system="", response_model=None):
        return LLMResponse(content="", usage=Usage())

    def classify_intent(self, question: str) -> str:
        return self.intent

    def generate_sql(self, question, schema_ddl, feedback, strategy="standard", history=None):
        self.sql_histories.append(list(history or []))
        return LLMResponse(
            content="",
            usage=Usage(prompt_tokens=4, completion_tokens=2, cost_usd=0.01),
            parsed=SQLCandidate(sql="SELECT COUNT(*) FROM customers", explanation="count"),
        )

    def answer_text(self, question, *, context="", history=None):
        self.text_histories.append(list(history or []))
        return LLMResponse(
            content="",
            usage=Usage(prompt_tokens=3, completion_tokens=2, cost_usd=0.02),
            parsed=TextAnswer(answer=self.answer),
        )

    def summarize_result(self, question, sql, columns, rows):
        if self.summary_error:
            raise RuntimeError("summary unavailable")
        return LLMResponse(
            content="",
            usage=Usage(prompt_tokens=5, completion_tokens=3, cost_usd=0.03),
            parsed=TextAnswer(answer="结果总结"),
        )


class FailingLLM(LLMClient):
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def generate(self, prompt, *, system="", response_model=None):
        self.calls += 1
        raise self.error


def _registry(*clients: tuple[str, LLMClient]) -> ProviderRegistry:
    names = {name: client for name, client in clients}
    configs = {
        name: ProviderConfig(
            name=name,
            api_key="test-key",
            base_url="http://example.test",
            model=f"{name}-model",
        )
        for name in ("deepseek", "qwen", "openai")
    }
    return ProviderRegistry(configs=configs, clients=names, default_provider="deepseek")


def _agent(
    llm: LLMClient,
    *,
    schema: FakeSchema | None = None,
    executor: FakeExecutor | None = None,
    enable_summary: bool = True,
    intent_router: bool = True,
) -> tuple[AgentLoop, FakeSchema, FakeExecutor]:
    schema = schema or FakeSchema()
    executor = executor or FakeExecutor()
    return (
        AgentLoop(
            llm=llm,
            executor=executor,
            validator=ResultValidator(),
            schema_retriever=schema,
            enable_router=intent_router,
            enable_summary=enable_summary,
        ),
        schema,
        executor,
    )


def test_query_agent_keeps_latest_five_turns_for_current_role_and_emits_events() -> None:
    scripted = ScriptedLLM(answer="unused")
    registry = _registry(("deepseek", scripted))
    routed = RoutedLLMClient(registry)
    agent, schema, executor = _agent(routed)
    events: list[tuple[str, dict]] = []
    history = [
        {"role": "analyst", "question": f"old-{i}", "answer": "a"}
        for i in range(7)
    ] + [{"role": "hr", "question": "must-not-leak", "answer": "a"}]

    result = agent.run(
        "统计客户数量",
        role="analyst",
        history=history,
        event_callback=lambda event, payload: events.append((event, payload)),
    )

    assert result.status == "done"
    assert result.role == "analyst"
    assert result.history_turns == 5
    assert [item["question"] for item in scripted.sql_histories[0]] == [
        "old-2",
        "old-3",
        "old-4",
        "old-5",
        "old-6",
    ]
    assert schema.calls == [("统计客户数量", "analyst")]
    assert executor.calls == [("SELECT COUNT(*) FROM customers", "analyst")]
    assert result.answer == "结果总结"
    assert result.provider == "deepseek"
    assert result.model == "deepseek-model"
    assert result.total_tokens == 14
    assert [event for event, _ in events][-2:] == ["result", "done"]
    assert any(event == "token" for event, _ in events)


@pytest.mark.parametrize(
    ("intent", "should_call_schema", "should_call_executor"),
    [("metadata", True, False), ("chat", False, False)],
)
def test_non_query_intents_use_text_answer_without_sql(
    intent: str,
    should_call_schema: bool,
    should_call_executor: bool,
) -> None:
    scripted = ScriptedLLM(intent=intent, answer="文本回答")
    agent, schema, executor = _agent(scripted)

    result = agent.run("请回答", role="readonly")

    assert result.status == "done"
    assert result.intent == intent
    assert result.answer == "文本回答"
    assert bool(schema.calls) is should_call_schema
    assert bool(executor.calls) is should_call_executor


def test_summary_failure_returns_fixed_fallback_without_losing_query_result() -> None:
    scripted = ScriptedLLM(summary_error=True)
    agent, _schema, _executor = _agent(scripted)

    result = agent.run("统计客户数量")

    assert result.status == "done"
    assert result.summary_fallback is True
    assert result.answer == "查询完成，结果为 3。"
    assert result.rows == [(3,)]


def test_provider_failover_only_applies_to_recoverable_failures() -> None:
    primary = FailingLLM(ConnectionError("network down"))
    backup = ScriptedLLM()
    registry = _registry(("deepseek", primary), ("qwen", backup))
    routed = RoutedLLMClient(registry)
    response = routed.generate("hello")

    assert response is not None
    assert routed.last_provider is not None
    assert routed.last_provider.name == "qwen"
    assert routed.active_provider == "qwen"
    assert primary.calls == 1

    bad_primary = FailingLLM(ValueError("invalid request"))
    bad_registry = _registry(("deepseek", bad_primary), ("qwen", ScriptedLLM()))
    with pytest.raises(ProviderError) as exc_info:
        RoutedLLMClient(bad_registry).generate("hello")
    assert exc_info.value.recoverable is False
    assert bad_primary.calls == 1


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    parsed = []
    for block in text.strip().split("\n\n"):
        lines = block.splitlines()
        if len(lines) < 2:
            continue
        event = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        parsed.append((event, data))
    return parsed


def test_chat_stream_exposes_structured_events_and_hides_provider_key() -> None:
    scripted = ScriptedLLM(answer="API 总结")
    registry = _registry(("deepseek", scripted))
    fake_executor = FakeExecutor()
    services = AppServices(
        registry=registry,
        executor=fake_executor,
        schema_retriever=FakeSchema(),
        access_config=load_access_config(),
    )

    with TestClient(create_app(services=services)) as client:
        response = client.post(
            "/api/chat/stream",
            json={
                "question": "统计客户数量",
                "role": "analyst",
                "provider": "deepseek",
                "history": [
                    {"role": "hr", "question": "other role", "answer": "hidden"},
                    {"role": "analyst", "question": "same role", "answer": "kept"},
                ],
            },
        )

    assert response.status_code == 200
    events = _parse_sse(response.text)
    names = [name for name, _payload in events]
    assert "stage" in names
    assert "token" in names
    assert names[-2:] == ["result", "done"]
    result_payload = next(payload["result"] for name, payload in events if name == "result")
    assert result_payload["status"] == "done"
    assert result_payload["provider"] == "deepseek"
    assert result_payload["role"] == "analyst"
    assert "test-key" not in response.text
    assert fake_executor.calls == [("SELECT COUNT(*) FROM customers", "analyst")]
