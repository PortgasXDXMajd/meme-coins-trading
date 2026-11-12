"""Builds the dossier: the numbered facts both sides of the debate must argue from."""

from __future__ import annotations

from datetime import datetime

from desk.config import Settings
from desk.domain.models import (
    Candidate,
    Dossier,
    Fact,
    FilterVerdict,
    RoundTrip,
    TokenSafety,
    TxnCounts,
    utcnow,
)

HOLDERS_LISTED = 10


def _money(value: float | None) -> str:
    return "unknown" if value is None else f"${value:,.0f}"


def _pct(value: float | None, digits: int = 1) -> str:
    return "unknown" if value is None else f"{value:.{digits}f}%"


def _txns(label: str, counts: TxnCounts) -> str:
    total = counts.buys + counts.sells
    share = f"{100 * counts.buys / total:.0f}% buys" if total else "no trades"
    return f"{label}: {counts.buys} buys / {counts.sells} sells ({share})"


def _market_facts(c: Candidate, now: datetime) -> list[tuple[str, str]]:
    liquidity = c.liquidity_usd or 0
    volume = c.volume_usd
    change = c.price_change_pct
    turnover = f"{(volume.h24 or 0) / liquidity:.1f}x" if liquidity else "unknown"
    return [
        ("market", f"Pair age {c.age_hours(now):.1f}h on {c.dex_id}."),
        (
            "market",
            f"Market cap {_money(c.market_cap_usd)}, FDV {_money(c.fdv_usd)}, "
            f"price ${c.price_usd:.10g}.",
        ),
        (
            "market",
            f"Pool liquidity {_money(c.liquidity_usd)} "
            f"({100 * liquidity / c.market_cap_usd:.1f}% of market cap).",
        ),
        (
            "market",
            f"Price change: 5m {_pct(change.m5)}, 1h {_pct(change.h1)}, "
            f"6h {_pct(change.h6)}, 24h {_pct(change.h24)}.",
        ),
        (
            "market",
            f"Volume: 5m {_money(volume.m5)}, 1h {_money(volume.h1)}, 6h {_money(volume.h6)}, "
            f"24h {_money(volume.h24)} (24h volume is {turnover} pool liquidity).",
        ),
        (
            "market",
            "Trades. "
            + "; ".join(
                [
                    _txns("5m", c.txns_m5),
                    _txns("1h", c.txns_h1),
                    _txns("6h", c.txns_h6),
                    _txns("24h", c.txns_h24),
                ]
            )
            + ".",
        ),
    ]


def _safety_facts(s: TokenSafety, settings: Settings) -> list[tuple[str, str]]:
    facts = [
        (
            "authority",
            f"Mint authority revoked: {s.mint_authority_revoked}. "
            f"Freeze authority revoked: {s.freeze_authority_revoked}. "
            f"Token program: {s.token_program}. Transfer fee: {_pct(s.transfer_fee_pct, 2)}.",
        ),
        ("liquidity", f"LP burned or locked: {_pct(s.lp_locked_pct)} (liquidity-weighted)."),
        (
            "holders",
            f"Top 10 holders excluding pools hold {_pct(s.top10_holders_pct)} "
            f"(desk limit {settings.max_top10_holders_pct:.0f}%). "
            f"Total holders: {s.total_holders if s.total_holders is not None else 'unknown'}.",
        ),
        (
            "holders",
            f"Creator {s.creator} holds {_pct(s.dev_holding_pct, 2)} "
            f"(desk limit {settings.max_dev_holding_pct:.0f}%). Launchpad: {s.launchpad}. "
            "Creator's earlier tokens on record: "
            f"{'unknown' if s.creator_prior_tokens is None else s.creator_prior_tokens}.",
        ),
    ]
    if s.top_holders:
        listed = "; ".join(
            f"{h.owner} {h.pct:.2f}%"
            + (f" [{h.label}]" if h.label else "")
            + (" [insider]" if h.insider else "")
            for h in s.top_holders[:HOLDERS_LISTED]
        )
        facts.append(("holders", f"Largest token accounts by owner: {listed}."))
    facts.append(
        (
            "insiders",
            f"RugCheck insider analysis flags {s.insiders_detected} wallets"
            + (
                "; networks: "
                + "; ".join(
                    f"{n.network_id} ({n.kind}, {n.wallets} wallets, "
                    f"holding {_pct(n.current_holding_pct)} of supply)"
                    for n in s.insider_networks
                )
                if s.insider_networks
                else "; no linked-wallet networks reported"
            )
            + ".",
        )
    )
    flags = "; ".join(f"{f.name} [{f.level}] {f.description}".strip() for f in s.risk_flags)
    facts.append(
        (
            "rugcheck",
            f"RugCheck normalised risk score {s.rugcheck_score} (higher is riskier). "
            f"Flags: {flags or 'none'}.",
        )
    )
    return facts


def _execution_facts(r: RoundTrip) -> list[tuple[str, str]]:
    return [
        (
            "execution",
            f"Quoted round trip (buy then sell ${r.size_usd:.0f}) costs {r.cost_pct:.2f}% "
            f"in fees, spread and price movement between the two quotes; "
            f"buy price impact {_pct(r.buy_price_impact_pct, 2)}, "
            f"sell price impact {_pct(r.sell_price_impact_pct, 2)}.",
        )
    ]


def build_dossier(
    verdict: FilterVerdict, settings: Settings, now: datetime | None = None
) -> Dossier:
    if not verdict.passed or verdict.safety is None or verdict.round_trip is None:
        raise ValueError("a dossier is only built for candidates that passed the Filter")
    entries = (
        _market_facts(verdict.candidate, now or utcnow())
        + _safety_facts(verdict.safety, settings)
        + _execution_facts(verdict.round_trip)
    )
    facts = tuple(
        Fact(id=f"F{number}", category=category, statement=statement)
        for number, (category, statement) in enumerate(entries, start=1)
    )
    return Dossier(
        candidate=verdict.candidate,
        safety=verdict.safety,
        round_trip=verdict.round_trip,
        facts=facts,
    )
