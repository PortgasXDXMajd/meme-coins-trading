"""Request pacing: stop-loss quotes must never queue behind the Filter's probes."""

from __future__ import annotations

import asyncio
import time

import httpx

from desk.data.http import RateLimiter
from desk.data.jupiter import Jupiter
from tests.conftest import load_fixture

INTERVAL = 0.05


async def test_urgent_caller_goes_ahead_of_queued_normal_callers():
    limiter = RateLimiter(INTERVAL)
    served: list[str] = []

    async def call(name: str, *, urgent: bool = False) -> None:
        await limiter.wait(urgent=urgent)
        served.append(name)

    await limiter.wait()  # the current slot is taken
    normal = [asyncio.create_task(call(f"normal-{n}")) for n in range(3)]
    await asyncio.sleep(0)
    urgent = asyncio.create_task(call("urgent", urgent=True))
    await asyncio.gather(*normal, urgent)

    assert served[0] == "urgent"
    assert sorted(served[1:]) == ["normal-0", "normal-1", "normal-2"]


async def test_urgent_calls_still_respect_the_rate_limit():
    limiter = RateLimiter(INTERVAL)
    started = time.monotonic()
    for _ in range(3):
        await limiter.wait(urgent=True)
    assert time.monotonic() - started >= 2 * INTERVAL * 0.9


async def test_stop_quote_jumps_ahead_of_a_filter_round_trip():
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.params["inputMint"])
        return httpx.Response(200, json=load_fixture("jup_order_sell.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        jupiter = Jupiter(client)
        jupiter._limiter = RateLimiter(INTERVAL)  # the real pacing is seconds apart

        probe = asyncio.create_task(jupiter.round_trip("ProbeMint", 20, 80))
        await asyncio.sleep(INTERVAL / 5)  # the probe's buy leg has gone out
        await jupiter.urgent().sell_quote("HeldMint", 1_000)
        await probe

    assert sent[1:] == ["HeldMint", "ProbeMint"]
