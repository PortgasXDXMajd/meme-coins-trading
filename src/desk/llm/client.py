"""Thin wrapper over the Anthropic SDK: one typed question in, one validated answer out.

Agents depend on the `StructuredLLM` protocol, so tests swap in a fake. Every
failure mode (API error, refusal, truncation, unparseable output) raises
`LLMFailure`, which callers must treat as "no answer", never as approval.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Any, Protocol

import anthropic
from anthropic.types.beta import (
    BetaMessageParam,
    BetaOutputConfigParam,
    BetaToolResultBlockParam,
)
from pydantic import BaseModel

from desk.config import Effort, Settings
from desk.domain.models import LLMUsage

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_OUTPUT_TOKENS = 16_000
# USD per million tokens (input, output).
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-2": (8.0, 40.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.1, 0.5),
}
TOOL_BUDGET_MESSAGE = "Tool budget used up. Answer now with the evidence already gathered."


class LLMFailure(Exception):
    """The model gave no usable answer."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[str]]

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "strict": True,
            "input_schema": self.input_schema,
        }


@dataclass(frozen=True)
class LLMResult[T: BaseModel]:
    output: T
    usage: LLMUsage


class StructuredLLM(Protocol):
    async def ask[T: BaseModel](
        self, *, system: str, user: str, output_type: type[T], tools: Sequence[Tool] = ()
    ) -> LLMResult[T]: ...


def load_prompt(name: str) -> str:
    return resources.files("desk.llm.prompts").joinpath(f"{name}.md").read_text(encoding="utf-8")


def debate_prompt(role: str, settings: Settings) -> str:
    """A debate role's prompt plus the desk baseline all three roles share."""
    baseline = load_prompt("desk_baseline").format(
        min_age_minutes=settings.min_pair_age_minutes,
        max_age_hours=settings.max_pair_age_hours,
        min_market_cap=settings.min_market_cap_usd,
        max_market_cap=settings.max_market_cap_usd,
        min_liquidity=settings.min_liquidity_usd,
        max_position_pct=settings.max_position_pct,
        stop_loss_pct=settings.stop_loss_pct,
        trail_activation_pct=settings.trail_activation_pct,
        trail_distance_pct=settings.trail_distance_pct,
    )
    return f"{load_prompt(role)}\n{baseline}"


class ClaudeLLM:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        *,
        model: str,
        effort: Effort,
        max_tool_rounds: int,
    ) -> None:
        self._client = client
        self._model = model
        self._effort: Effort = effort
        self._max_tool_rounds = max_tool_rounds

    async def ask[T: BaseModel](
        self, *, system: str, user: str, output_type: type[T], tools: Sequence[Tool] = ()
    ) -> LLMResult[T]:
        messages: list[BetaMessageParam] = [{"role": "user", "content": user}]
        handlers = {tool.name: tool.handler for tool in tools}
        usage = LLMUsage()

        # Two rounds beyond the budget: one to be told it is spent, one to answer.
        for round_number in range(self._max_tool_rounds + 2):
            response = await self._request(system, messages, output_type, tools)
            usage += self._usage(response)

            if response.stop_reason == "tool_use":
                budget_left = round_number < self._max_tool_rounds
                messages.append({"role": "assistant", "content": response.content})
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            await self._run_tool(block, handlers, budget_left)
                            for block in response.content
                            if block.type == "tool_use"
                        ],
                    }
                )
                continue

            if response.stop_reason != "end_turn":
                raise LLMFailure(f"model stopped with {response.stop_reason!r}")
            if response.parsed_output is None:
                raise LLMFailure("model returned no parseable output")
            return LLMResult(output=response.parsed_output, usage=usage)

        raise LLMFailure("model kept calling tools past its budget")

    async def _request(
        self,
        system: str,
        messages: list[BetaMessageParam],
        output_type: type[BaseModel],
        tools: Sequence[Tool],
    ) -> Any:
        kwargs: dict[str, Any] = {}
        output_config: BetaOutputConfigParam = {"effort": self._effort}
        if tools:
            kwargs["tools"] = [tool.definition() for tool in tools]
        try:
            return await self._client.beta.messages.parse(
                model=self._model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system,
                messages=messages,
                output_format=output_type,
                output_config=output_config,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                **kwargs,
            )
        except anthropic.APIConnectionError as exc:
            raise LLMFailure(f"could not reach the Claude API: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMFailure(f"Claude API returned {exc.status_code}: {exc.message}") from exc
        except ValueError as exc:  # structured output failed validation
            raise LLMFailure(f"model output did not match the schema: {exc}") from exc

    @staticmethod
    async def _run_tool(
        block: Any,
        handlers: dict[str, Callable[[dict[str, Any]], Awaitable[str]]],
        budget_left: bool,
    ) -> BetaToolResultBlockParam:
        def result(content: str, *, is_error: bool = False) -> BetaToolResultBlockParam:
            return {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content,
                "is_error": is_error,
            }

        if not budget_left:
            return result(TOOL_BUDGET_MESSAGE, is_error=True)
        handler = handlers.get(block.name)
        if handler is None:
            return result(f"Unknown tool {block.name!r}.", is_error=True)
        try:
            return result(await handler(dict(block.input)))
        except Exception as exc:  # a broken tool must not sink the debate
            log.warning("tool %s failed: %s", block.name, exc)
            return result(f"Tool failed: {exc}", is_error=True)

    def _usage(self, response: Any) -> LLMUsage:
        usage = response.usage
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        input_tokens = usage.input_tokens + cache_write + cache_read
        input_price, output_price = PRICES.get(self._model, PRICES["claude-opus-5-5"])
        cost = (
            usage.input_tokens * input_price
            + cache_write * input_price * 1.25
            + cache_read * input_price * 0.1
            + usage.output_tokens * output_price
        ) / 1_000_000
        return LLMUsage(input_tokens=input_tokens, output_tokens=usage.output_tokens, cost_usd=cost)


def to_json(value: Any) -> str:
    """Stable JSON for prompts."""
    return json.dumps(value, indent=2, sort_keys=True, default=str)
