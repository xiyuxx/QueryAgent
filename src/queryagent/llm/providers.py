"""Configured OpenAI-compatible model providers and request-scoped failover."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Iterable

import httpx

from .base import LLMClient, LLMResponse
from .errors import ProviderError
from .openai_compat import OpenAICompatClient


PROVIDER_ORDER = ("deepseek", "qwen", "openai")


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    api_key: str
    base_url: str
    model: str

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def public_dict(self) -> dict[str, str | bool]:
        return {
            "name": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "configured": self.configured,
        }


@dataclass(frozen=True)
class ProviderAttempt:
    provider: str
    model: str
    error: str | None = None
    fallback: bool = False


@dataclass
class ProviderCallResult:
    response: LLMResponse
    provider: ProviderConfig
    attempts: list[ProviderAttempt]


_DEFAULTS = {
    "deepseek": ("https://api.deepseek.com", "deepseek-chat"),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
}


def provider_config_from_environment(name: str, environ: dict[str, str] | None = None) -> ProviderConfig:
    env = os.environ if environ is None else environ
    key = name.upper()
    default_base, default_model = _DEFAULTS[name]
    return ProviderConfig(
        name=name,
        api_key=env.get(f"{key}_API_KEY", "").strip(),
        base_url=env.get(f"{key}_BASE_URL", default_base).strip().rstrip("/"),
        model=env.get(f"{key}_MODEL", default_model).strip(),
    )


class ProviderRegistry:
    """Build configured clients and perform one request-scoped failover."""

    def __init__(
        self,
        *,
        configs: dict[str, ProviderConfig] | None = None,
        clients: dict[str, LLMClient] | None = None,
        default_provider: str | None = None,
        client_factory: Callable[[ProviderConfig], LLMClient] | None = None,
    ) -> None:
        self.configs = (
            configs
            if configs is not None
            else {name: provider_config_from_environment(name) for name in PROVIDER_ORDER}
        )
        self._clients = clients if clients is not None else {}
        self._client_factory = client_factory or self._build_client
        configured_default = (default_provider or os.environ.get("QUERYAGENT_PROVIDER", "deepseek")).lower()
        configured_names = {
            name for name, config in self.configs.items() if config.configured
        }
        self.default_provider = (
            configured_default
            if configured_default in configured_names
            else next(
                (name for name in PROVIDER_ORDER if name in configured_names),
                configured_default,
            )
        )

    @staticmethod
    def _build_client(config: ProviderConfig) -> LLMClient:
        return OpenAICompatClient(
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            # Per-provider pricing can be configured in a later phase; these
            # defaults preserve the existing cost accounting contract.
        )

    def available(self) -> list[ProviderConfig]:
        return [
            config
            for name in PROVIDER_ORDER
            if (config := self.configs.get(name)) is not None and config.configured
        ]

    def public_status(self) -> list[dict[str, str | bool]]:
        return [config.public_dict() for config in self.available()]

    def get(self, name: str | None = None) -> tuple[ProviderConfig, LLMClient]:
        provider_name = (name or self.default_provider).lower()
        config = self.configs.get(provider_name)
        if config is None or not config.configured:
            raise ProviderError(
                f"provider {provider_name!r} is not configured",
                recoverable=False,
            )
        if provider_name not in self._clients:
            self._clients[provider_name] = self._client_factory(config)
        return config, self._clients[provider_name]

    def attempt_order(self, selected: str | None) -> list[str]:
        chosen = (selected or self.default_provider).lower()
        chosen_config = self.configs.get(chosen)
        if selected is not None and (
            chosen_config is None or not chosen_config.configured
        ):
            # An explicitly selected but unavailable provider is a local
            # configuration error, so it must not silently fail over.
            return [chosen]
        names: list[str] = []
        for name in (chosen, *PROVIDER_ORDER):
            if name not in names and name in self.configs and self.configs[name].configured:
                names.append(name)
        return names

    @staticmethod
    def is_recoverable(exc: BaseException) -> bool:
        if isinstance(exc, ProviderError):
            return exc.recoverable
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return True
        if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            return status_code >= 500
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        return isinstance(status_code, int) and status_code >= 500

    def call(
        self,
        selected: str | None,
        operation: Callable[[LLMClient], LLMResponse],
    ) -> ProviderCallResult:
        attempts: list[ProviderAttempt] = []
        names = self.attempt_order(selected)
        if not names:
            raise ProviderError("没有已配置的模型 Provider", recoverable=False)
        for index, name in enumerate(names):
            try:
                config, client = self.get(name)
            except ProviderError as exc:
                attempts.append(
                    ProviderAttempt(
                        provider=name,
                        model=self.configs[name].model if name in self.configs else "",
                        error=str(exc),
                        fallback=index > 0,
                    )
                )
                # A selected provider that is not configured is a user/config
                # error, not a transient upstream error.
                raise
            try:
                response = operation(client)
            except Exception as exc:  # noqa: BLE001 - classify upstream failures centrally
                recoverable = self.is_recoverable(exc)
                attempts.append(
                    ProviderAttempt(
                        provider=name,
                        model=config.model,
                        error=f"{type(exc).__name__}: {exc}",
                        fallback=index > 0,
                    )
                )
                if recoverable and index + 1 < len(names):
                    continue
                if isinstance(exc, ProviderError):
                    raise
                raise ProviderError(
                    f"{name} 调用失败：{type(exc).__name__}: {exc}",
                    recoverable=recoverable,
                    status_code=getattr(getattr(exc, "response", None), "status_code", None),
                ) from exc
            attempts.append(
                ProviderAttempt(provider=name, model=config.model, fallback=index > 0)
            )
            return ProviderCallResult(response=response, provider=config, attempts=attempts)
        raise ProviderError("所有已配置 Provider 均调用失败", recoverable=True)


def attempts_to_dict(attempts: Iterable[ProviderAttempt]) -> list[dict[str, str | bool | None]]:
    return [
        {
            "provider": attempt.provider,
            "model": attempt.model,
            "error": attempt.error,
            "fallback": attempt.fallback,
        }
        for attempt in attempts
    ]
