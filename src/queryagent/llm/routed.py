"""LLMClient adapter that routes every operation through ProviderRegistry."""
from __future__ import annotations

from typing import Any, Callable, Sequence, Type

from pydantic import BaseModel

from .base import (
    INTENT_SYSTEM,
    VALUE_EXTRACT_SYSTEM,
    IntentResult,
    LLMClient,
    LLMResponse,
    ValueCandidates,
    build_intent_prompt,
    build_value_prompt,
)
from .providers import ProviderAttempt, ProviderConfig, ProviderRegistry


class RoutedLLMClient(LLMClient):
    """Preserve the LLMClient contract while adding request-scoped failover."""

    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        selected_provider: str | None = None,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.registry = registry
        self.selected_provider = selected_provider or registry.default_provider
        # ``selected_provider`` is the user's preference. ``active_provider``
        # may move to a fallback for the lifetime of this one Agent request.
        self.active_provider = self.selected_provider
        self.event_callback = event_callback
        self.last_provider: ProviderConfig | None = None
        self.last_attempts: list[ProviderAttempt] = []
        self.provider_history: list[ProviderConfig] = []
        self.attempt_history: list[list[ProviderAttempt]] = []

    @property
    def model(self) -> str | None:
        return self.last_provider.model if self.last_provider else None

    def set_event_callback(
        self,
        callback: Callable[[str, dict[str, Any]], None] | None,
    ) -> None:
        self.event_callback = callback

    def _route(self, operation: Callable[[LLMClient], LLMResponse]) -> LLMResponse:
        routed = self.registry.call(self.active_provider, operation)
        self.last_provider = routed.provider
        self.active_provider = routed.provider.name
        self.last_attempts = routed.attempts
        self.provider_history.append(routed.provider)
        self.attempt_history.append(routed.attempts)
        if self.event_callback is not None:
            self.event_callback(
                "provider",
                {
                    "provider": routed.provider.name,
                    "model": routed.provider.model,
                    "fallback": len(routed.attempts) > 1,
                    "attempts": [
                        {
                            "provider": attempt.provider,
                            "model": attempt.model,
                            "error": attempt.error,
                            "fallback": attempt.fallback,
                        }
                        for attempt in routed.attempts
                    ],
                },
            )
        return routed.response

    def generate(
        self,
        prompt: str,
        *,
        system: str = "",
        response_model: Type[BaseModel] | None = None,
    ) -> LLMResponse:
        return self._route(
            lambda client: client.generate(
                prompt,
                system=system,
                response_model=response_model,
            )
        )

    def generate_sql(
        self,
        question: str,
        schema_ddl: str,
        feedback: list[str],
        strategy: str = "standard",
        history: Sequence[dict] | None = None,
    ) -> LLMResponse:
        return self._route(
            lambda client: client.generate_sql(
                question,
                schema_ddl,
                feedback,
                strategy=strategy,
                history=history,
            )
        )

    def audit(self, question: str, sql: str, result_preview: str) -> LLMResponse:
        return self._route(lambda client: client.audit(question, sql, result_preview))

    def answer_text(
        self,
        question: str,
        *,
        context: str = "",
        history: Sequence[dict] | None = None,
    ) -> LLMResponse:
        return self._route(
            lambda client: client.answer_text(
                question,
                context=context,
                history=history,
            )
        )

    def summarize_result(
        self,
        question: str,
        sql: str,
        columns: list[str],
        rows: list[tuple],
    ) -> LLMResponse:
        return self._route(
            lambda client: client.summarize_result(question, sql, columns, rows)
        )

    def classify_intent(self, question: str) -> str:
        response = self._route(
            lambda client: client.generate(
                build_intent_prompt(question),
                system=INTENT_SYSTEM,
                response_model=IntentResult,
            )
        )
        return str(getattr(response.parsed, "intent", "query")) if response.parsed else "query"

    def extract_values(self, question: str, schema_context: str = "") -> list[str]:
        response = self._route(
            lambda client: client.generate(
                build_value_prompt(question, schema_context),
                system=VALUE_EXTRACT_SYSTEM,
                response_model=ValueCandidates,
            )
        )
        return list(getattr(response.parsed, "values", [])) if response.parsed else []
