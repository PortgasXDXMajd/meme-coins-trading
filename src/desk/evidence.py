"""Read-only investigation tools for Bear and Analyst.

Every successful tool call is recorded in a shared ledger as a citable fact
(E1, E2, ...), so the referee sees exactly the evidence the debaters saw.
"""

from __future__ import annotations

import re
from typing import Any

from desk.data.dexscreener import DexScreener
from desk.data.http import DataError
from desk.data.jupiter import Jupiter
from desk.data.solana_rpc import SolanaRpc
from desk.domain.models import Dossier, Fact
from desk.llm.client import Tool

BASE58_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
MIN_EXIT_PCT = 0.05
MAX_EXIT_PCT = 10.0


class EvidenceLedger:
    def __init__(self) -> None:
        self._facts: list[Fact] = []

    def record(self, category: str, statement: str) -> Fact:
        fact = Fact(id=f"E{len(self._facts) + 1}", category=category, statement=statement)
        self._facts.append(fact)
        return fact

    @property
    def facts(self) -> tuple[Fact, ...]:
        return tuple(self._facts)

    def render(self) -> str:
        if not self._facts:
            return "(none yet)"
        return "\n".join(f"[{f.id}] ({f.category}) {f.statement}" for f in self._facts)


def _object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def build_evidence_tools(
    dossier: Dossier,
    ledger: EvidenceLedger,
    dex: DexScreener,
    jupiter: Jupiter,
    rpc: SolanaRpc,
) -> list[Tool]:
    candidate = dossier.candidate
    supply_raw = dossier.safety.supply_raw

    def cite(category: str, statement: str) -> str:
        fact = ledger.record(category, statement)
        return f"Recorded as [{fact.id}]: {fact.statement}"

    async def refresh_market(_: dict[str, Any]) -> str:
        try:
            fresh = await dex.snapshot(candidate.mint)
        except DataError as exc:
            return f"Unavailable: {exc}"
        if fresh is None:
            return "Unavailable: the pair no longer returns data."
        move = 100 * (fresh.price_usd / candidate.price_usd - 1)
        m5 = fresh.txns_m5
        return cite(
            "market-now",
            f"Fresh snapshot: price ${fresh.price_usd:.10g} ({move:+.1f}% vs dossier), "
            f"liquidity ${fresh.liquidity_usd or 0:,.0f}, market cap ${fresh.market_cap_usd:,.0f}, "
            f"last 5m {m5.buys} buys / {m5.sells} sells, "
            f"5m volume ${fresh.volume_usd.m5 or 0:,.0f}, 5m price change "
            f"{fresh.price_change_pct.m5 if fresh.price_change_pct.m5 is not None else 0:+.1f}%.",
        )

    async def inspect_wallet(args: dict[str, Any]) -> str:
        address = str(args["address"]).strip()
        if not BASE58_ADDRESS.match(address):
            return "Not a valid Solana address."
        try:
            profile = await rpc.wallet_profile(address)
        except DataError as exc:
            return f"Unavailable: {exc}"
        count = f"{profile.recent_tx_count}{'+' if profile.tx_count_is_capped else ''}"
        oldest = (
            f"{profile.oldest_seen_tx_at:%Y-%m-%d %H:%M} UTC"
            if profile.oldest_seen_tx_at
            else "none"
        )
        newest = f"{profile.newest_tx_at:%Y-%m-%d %H:%M} UTC" if profile.newest_tx_at else "none"
        qualifier = "oldest of the latest 1000" if profile.tx_count_is_capped else "first ever"
        return cite(
            "wallet",
            f"Wallet {address}: {profile.sol_balance:.3f} SOL, {count} transactions, "
            f"{qualifier} at {oldest}, most recent at {newest}.",
        )

    async def quote_exit(args: dict[str, Any]) -> str:
        if supply_raw is None:
            return "Unavailable: token supply unknown."
        pct = float(args["percent_of_supply"])
        if not MIN_EXIT_PCT <= pct <= MAX_EXIT_PCT:
            return f"percent_of_supply must be between {MIN_EXIT_PCT} and {MAX_EXIT_PCT}."
        try:
            order = await jupiter.sell_quote(candidate.mint, int(supply_raw * pct / 100))
        except DataError as exc:
            return f"Unavailable: {exc}"
        impact = "unknown" if order.price_impact_pct is None else f"{order.price_impact_pct:.2f}%"
        return cite(
            "exit-depth",
            f"Selling {pct:g}% of supply in one swap would return ${order.out_usd:,.0f} "
            f"for tokens marked at ${order.in_usd:,.0f} (price impact {impact}).",
        )

    return [
        Tool(
            name="refresh_market",
            description=(
                "Fetch the token's current price, liquidity and last-5-minute trading, "
                "to see how it moved since the dossier was built."
            ),
            input_schema=_object_schema({}),
            handler=refresh_market,
        ),
        Tool(
            name="inspect_wallet",
            description=(
                "Look up a Solana wallet (a top holder or the creator): SOL balance, "
                "transaction count and how long it has been active. Fresh, thinly used "
                "wallets among top holders suggest bundled or sybil supply."
            ),
            input_schema=_object_schema(
                {"address": {"type": "string", "description": "Wallet address (owner)."}}
            ),
            handler=inspect_wallet,
        ),
        Tool(
            name="quote_exit",
            description=(
                "Get a live quote for dumping a given percentage of total supply in one "
                "swap, to measure how much a large holder exiting would move the price."
            ),
            input_schema=_object_schema(
                {
                    "percent_of_supply": {
                        "type": "number",
                        "description": f"Between {MIN_EXIT_PCT} and {MAX_EXIT_PCT}.",
                    }
                }
            ),
            handler=quote_exit,
        ),
    ]
