"""RugCheck: authorities, LP lock status, holders, creator and insider networks."""

from __future__ import annotations

from typing import Any

import httpx

from desk.data.http import RateLimiter, request_json
from desk.domain.models import Holder, InsiderNetwork, RiskFlag, TokenSafety

BASE_URL = "https://api.rugcheck.xyz/v1"
POOL_ACCOUNT_TYPES = {"AMM", "LOCKER"}
BURN_OWNERS = {
    "1nc1nerator11111111111111111111111111111111",
    "11111111111111111111111111111111",
}
TOP_N = 10
# Token-2022 extensions that let someone else move, freeze or block your tokens.
DANGEROUS_EXTENSIONS = ("permanentDelegate", "transferHook", "pausableConfig")


def _lp_locked_pct(markets: list[dict[str, Any]]) -> float | None:
    """Liquidity-weighted share of LP that is burned or locked, across all pools."""
    total_usd = 0.0
    locked_usd = 0.0
    for market in markets:
        lp = market.get("lp") or {}
        total_usd += (lp.get("quoteUSD") or 0) + (lp.get("baseUSD") or 0)
        locked_usd += lp.get("lpLockedUSD") or 0
    if total_usd <= 0:
        return None
    return min(100.0, 100 * locked_usd / total_usd)


def _holders(report: dict[str, Any]) -> tuple[Holder, ...]:
    known = report.get("knownAccounts") or {}
    pool_token_accounts = {
        market.get(key)
        for market in report.get("markets") or []
        for key in ("liquidityA", "liquidityB")
    }
    holders = []
    for raw in report.get("topHolders") or []:
        owner = raw.get("owner") or ""
        label = (known.get(owner) or {}).get("type")
        if raw.get("address") in pool_token_accounts and label is None:
            label = "AMM"
        if owner in BURN_OWNERS:
            label = "BURN"
        holders.append(
            Holder(
                owner=owner,
                pct=float(raw.get("pct") or 0),
                insider=bool(raw.get("insider")),
                label=label,
            )
        )
    return tuple(holders)


def _is_circulating(holder: Holder) -> bool:
    return holder.label not in POOL_ACCOUNT_TYPES and holder.label != "BURN"


def _dangerous_extensions(report: dict[str, Any]) -> tuple[str, ...]:
    extensions = report.get("token_extensions") or {}
    found = [name for name in DANGEROUS_EXTENSIONS if extensions.get(name)]
    if extensions.get("nonTransferable"):
        found.append("nonTransferable")
    default_state = extensions.get("defaultAccountState")
    if default_state and "frozen" in str(default_state).lower():
        found.append("defaultAccountState=frozen")
    return tuple(found)


def parse_safety(report: dict[str, Any]) -> TokenSafety:
    """Normalise a RugCheck report. Anything absent stays None, which Filter kills."""
    mint = report.get("mint", "")
    token = report.get("token")
    supply = float(token.get("supply") or 0) if token else 0.0

    holders = _holders(report)
    circulating = [h for h in holders if _is_circulating(h)]
    top10_pct = sum(h.pct for h in circulating[:TOP_N]) if holders else None

    creator = report.get("creator") or None
    creator_balance = report.get("creatorBalance")
    dev_pct = None
    if creator and creator_balance is not None and supply > 0:
        dev_pct = 100 * float(creator_balance) / supply

    transfer_fee = report.get("transferFee")
    networks = tuple(
        InsiderNetwork(
            network_id=str(net.get("id", "?")),
            wallets=int(net.get("size") or 0),
            kind=str(net.get("type", "?")),
            current_holding_pct=(
                100 * float(net["currentHolding"]) / supply
                if supply > 0 and net.get("currentHolding") is not None
                else None
            ),
        )
        for net in report.get("insiderNetworks") or []
    )
    creator_tokens = report.get("creatorTokens")

    return TokenSafety(
        mint=mint,
        token_program=report.get("tokenProgram"),
        supply_raw=int(supply) if supply > 0 else None,
        mint_authority_revoked=None if token is None else token.get("mintAuthority") is None,
        freeze_authority_revoked=None if token is None else token.get("freezeAuthority") is None,
        lp_locked_pct=_lp_locked_pct(report.get("markets") or []),
        top10_holders_pct=top10_pct,
        dev_holding_pct=dev_pct,
        creator=creator,
        transfer_fee_pct=None if transfer_fee is None else float(transfer_fee.get("pct") or 0),
        dangerous_extensions=_dangerous_extensions(report),
        rugged=bool(report.get("rugged")),
        total_holders=report.get("totalHolders"),
        top_holders=holders,
        insiders_detected=report.get("graphInsidersDetected"),
        insider_networks=networks,
        risk_flags=tuple(
            RiskFlag(
                name=str(risk.get("name", "?")),
                level=str(risk.get("level", "?")),
                description=str(risk.get("description", "")),
            )
            for risk in report.get("risks") or []
        ),
        rugcheck_score=report.get("score_normalised"),
        launchpad=(report.get("launchpad") or {}).get("name"),
        creator_prior_tokens=None if creator_tokens is None else len(creator_tokens),
    )


class RugCheck:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._limiter = RateLimiter(1.0)

    async def safety(self, mint: str) -> TokenSafety:
        report = await request_json(
            self._client, "GET", f"{BASE_URL}/tokens/{mint}/report", limiter=self._limiter
        )
        return parse_safety(report)
