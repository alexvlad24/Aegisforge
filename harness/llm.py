"""LLM client: one provider (OpenAI, SPEC §2), usage accounting, budget enforcement.

Prompt caching. OpenAI caches prompt prefixes automatically (>= 1024 tokens) and has no explicit
`cache_control` breakpoints. The equivalent discipline here:
  * the request is [system prompt, task message] + append-only suffix, tools in a fixed order,
    so every turn's request is a byte-prefix-extension of the previous one;
  * `prompt_cache_key` is the task id (stable across turns, runs and replays; never the run id);
  * cache hits are read back from usage.prompt_tokens_details.cached_tokens and priced at the
    cached rate.

The API key is read through harness.config.secret only when a live transport is built, so a
replay never constructs the provider client.
"""

from __future__ import annotations

import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.budget import Budget, RunLimits
from harness.config import ROOT, secret
from harness.events import Usage
from harness.replay import Transport, TransportResponse, request_key
from harness.store import RunState

CONFIG_DIR = ROOT / "config"
MODELS_TOML = CONFIG_DIR / "models.toml"


# ------------------------------------------------------------------------------------ config


@dataclass(frozen=True)
class Price:
    input: float  # USD per 1M prompt tokens (uncached)
    cached_input: float  # USD per 1M cached prompt tokens
    output: float  # USD per 1M completion tokens

    def cost(self, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
        uncached = max(input_tokens - cached_tokens, 0)
        return (
            uncached * self.input + cached_tokens * self.cached_input + output_tokens * self.output
        ) / 1_000_000


@dataclass(frozen=True)
class RoleConfig:
    model: str
    temperature: float
    max_completion_tokens: int


@dataclass(frozen=True)
class ModelsConfig:
    provider: str
    roles: dict[str, RoleConfig]
    prices: dict[str, Price]
    total_usd_cap: float
    run_defaults: RunLimits

    def price_for(self, model: str) -> Price:
        try:
            return self.prices[model]
        except KeyError:
            raise KeyError(f"no price for model {model!r} in config/models.toml") from None


def load_models_config(path: Path = MODELS_TOML) -> ModelsConfig:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    if raw.get("provider") != "openai":
        raise ValueError("only provider = 'openai' is supported (SPEC §2)")
    roles = {
        name: RoleConfig(
            model=str(r["model"]),
            temperature=float(r.get("temperature", 0.0)),
            max_completion_tokens=int(r.get("max_completion_tokens", 2048)),
        )
        for name, r in raw["roles"].items()
    }
    prices = {
        model: Price(float(p["input"]), float(p["cached_input"]), float(p["output"]))
        for model, p in raw["prices"].items()
    }
    b = raw["budget"]
    d = b["run_defaults"]
    defaults = RunLimits(
        max_usd=float(d["max_usd"]),
        max_tokens=int(d["max_tokens"]),
        max_steps=int(d["max_steps"]),
        max_wall_s=float(d["max_wall_s"]),
    )
    return ModelsConfig(
        provider=raw["provider"],
        roles=roles,
        prices=prices,
        total_usd_cap=float(b["total_usd_cap"]),
        run_defaults=defaults,
    )


# --------------------------------------------------------------------------------- transport


class OpenAITransport:
    """Live provider calls. Built lazily so no client exists unless a request needs the network."""

    def __init__(self, timeout_s: float = 120.0, max_retries: int = 2) -> None:
        from openai import OpenAI

        self._client = OpenAI(
            api_key=secret("OPENAI_API_KEY"), timeout=timeout_s, max_retries=max_retries
        )

    def complete(self, request: dict[str, Any]) -> TransportResponse:
        completion = self._client.chat.completions.create(**request)
        body: dict[str, Any] = completion.model_dump(mode="json", exclude_none=True)
        return TransportResponse(body=body, live=True)


class LazyOpenAITransport:
    """Defers OpenAITransport construction (and the key lookup) to the first live request."""

    def __init__(self) -> None:
        self._inner: OpenAITransport | None = None

    def complete(self, request: dict[str, Any]) -> TransportResponse:
        if self._inner is None:
            self._inner = OpenAITransport()
        return self._inner.complete(request)


# ------------------------------------------------------------------------------------ client


@dataclass(frozen=True)
class ChatResult:
    request_hash: str
    message: dict[str, Any]  # normalized assistant message, safe to send back verbatim
    finish_reason: str | None
    model: str
    usage: Usage
    billed: bool
    latency_ms: float


def usage_from_body(body: dict[str, Any], price: Price) -> Usage:
    u = body.get("usage") or {}
    input_tokens = int(u.get("prompt_tokens", 0))
    output_tokens = int(u.get("completion_tokens", 0))
    cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    return Usage(
        input_tokens=input_tokens,
        cached_tokens=cached,
        output_tokens=output_tokens,
        cost_usd=price.cost(input_tokens, cached, output_tokens),
    )


def normalize_assistant_message(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields the next request needs, in a fixed shape (stable cassette keys)."""
    message: dict[str, Any] = {"role": "assistant", "content": raw.get("content")}
    calls = raw.get("tool_calls") or []
    if calls:
        message["tool_calls"] = [
            {
                "id": c["id"],
                "type": "function",
                "function": {
                    "name": c["function"]["name"],
                    "arguments": c["function"]["arguments"],
                },
            }
            for c in calls
        ]
    return message


class LLMClient:
    def __init__(
        self,
        transport: Transport,
        role: RoleConfig,
        price: Price,
        budget: Budget,
        cache_key: str,
    ) -> None:
        self.transport = transport
        self.role = role
        self.price = price
        self.budget = budget
        self.cache_key = cache_key

    def build_request(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "model": self.role.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "temperature": self.role.temperature,
            "max_completion_tokens": self.role.max_completion_tokens,
            "prompt_cache_key": self.cache_key,
        }

    def chat(
        self,
        state: RunState,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatResult:
        self.budget.check_before_call(state)  # raises BudgetExceeded; nothing is sent
        request = self.build_request(messages, tools)
        started = time.perf_counter()
        response = self.transport.complete(request)
        latency_ms = (time.perf_counter() - started) * 1000
        body = response.body
        choice = body["choices"][0]
        return ChatResult(
            request_hash=request_key(request),
            message=normalize_assistant_message(choice["message"]),
            finish_reason=choice.get("finish_reason"),
            model=str(body.get("model", self.role.model)),
            usage=usage_from_body(body, self.price),
            billed=response.live,
            latency_ms=latency_ms,
        )
