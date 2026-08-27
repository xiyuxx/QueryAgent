"""OpenAI 兼容客户端（闭源 API / DeepSeek / Qwen 均走此实现）。

用 httpx 直连，避免重依赖。Structured Output 通过 response_format=json_object
+ 系统提示注入 JSON Schema 实现（比 OpenAI 专属的 json_schema strict 模式兼容面更广，
DeepSeek 不支持 strict json_schema）。

成本按可配置的每千 token 单价估算；默认用 DeepSeek-chat 官方价
（输入 $0.27/M、输出 $1.10/M），v4-flash 实际单价以官方为准，可经构造参数覆盖。
未配置 API key 时不可用；离线评测用 MockLLM。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Optional, Type

import httpx
from pydantic import BaseModel

from .base import LLMClient, LLMResponse, Usage


def _extract_json(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    return content.strip()


class OpenAICompatClient(LLMClient):
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str = "deepseek-v4-flash",
        timeout_s: float = 120.0,
        max_tokens: int = 2048,
        temperature: float | None = None,
        input_price_per_1k: float = 0.00027,  # DeepSeek-chat 官方价（估算）
        output_price_per_1k: float = 0.0011,  # DeepSeek-chat 官方价（估算）
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.deepseek.com"
        ).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.input_price_per_1k = input_price_per_1k
        self.output_price_per_1k = output_price_per_1k

    def generate(
        self, prompt: str, *, system: str = "", response_model: Optional[Type[BaseModel]] = None
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")

        full_system = system
        if response_model is not None:
            schema = response_model.model_json_schema()
            full_system = (
                full_system
                + "\n\n输出必须是一个 JSON 对象，且严格符合以下 JSON Schema：\n"
                + json.dumps(schema, ensure_ascii=False)
            )

        messages: list[dict[str, str]] = []
        if full_system:
            messages.append({"role": "system", "content": full_system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if response_model is not None:
            payload["response_format"] = {"type": "json_object"}

        t0 = time.perf_counter()
        resp = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
            timeout=self.timeout_s,
        )
        resp.raise_for_status()
        data = resp.json()
        latency_ms = (time.perf_counter() - t0) * 1000.0

        choice = data["choices"][0]
        content = choice["message"]["content"] or ""
        usage_raw = data.get("usage", {})
        pt = usage_raw.get("prompt_tokens", 0)
        ct = usage_raw.get("completion_tokens", 0)
        cost = pt / 1000.0 * self.input_price_per_1k + ct / 1000.0 * self.output_price_per_1k
        usage = Usage(prompt_tokens=pt, completion_tokens=ct, cost_usd=cost)

        parsed = None
        if response_model is not None:
            if not content:
                raise ValueError("empty completion content (reasoning may have exhausted max_tokens)")
            parsed = response_model.model_validate_json(_extract_json(content))
        return LLMResponse(content=content, usage=usage, latency_ms=latency_ms, parsed=parsed)
