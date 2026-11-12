"""Parsers, checked against responses recorded from the live APIs."""

from __future__ import annotations

import pytest

from desk.data.dexscreener import best_pair_per_token, parse_pair
from desk.data.geckoterminal import base_token_mints
from desk.data.http import DataError
from desk.data.jupiter import parse_order, usd_to_lamports
from desk.data.rugcheck import parse_safety
from tests.conftest import load_fixture

DOGGLE = "E8qaeXz7CF2auJebXaQkLZyrFPfRQft3KrGgvRvwpump"


def test_dexscreener_pairs_parse_into_candidates():
    candidates = best_pair_per_token(load_fixture("dex_tokens.json"))
    doggle = candidates[DOGGLE]
    assert doggle.symbol == "DOGGLE"
    assert doggle.dex_id == "pumpswap"
    assert doggle.market_cap_usd > 0
    assert doggle.liquidity_usd and doggle.liquidity_usd > 0
    assert 50 < doggle.sol_price_usd < 500  # derived from priceUsd / priceNative


def test_pairs_missing_essentials_are_dropped():
    raw = next(p for p in load_fixture("dex_tokens.json") if p["baseToken"]["address"] == DOGGLE)
    assert parse_pair(raw) is not None
    assert parse_pair({**raw, "pairCreatedAt": None}) is None
    assert parse_pair({**raw, "marketCap": None}) is None
    assert parse_pair({**raw, "chainId": "ethereum"}) is None
    assert parse_pair({}) is None


def test_geckoterminal_extracts_base_token_mints():
    mints = base_token_mints(load_fixture("gecko_trending.json"))
    assert len(mints) > 5
    assert all(not m.startswith("solana_") for m in mints)


def test_rugcheck_report_parses_into_safety():
    safety = parse_safety(load_fixture("rug_report.json"))
    assert safety.mint == DOGGLE
    assert safety.mint_authority_revoked is True
    assert safety.freeze_authority_revoked is True
    assert safety.lp_locked_pct == pytest.approx(100, abs=0.1)
    assert safety.dev_holding_pct == 0
    assert safety.transfer_fee_pct == 0
    assert safety.dangerous_extensions == ()
    assert safety.supply_raw == 992515108455497
    assert safety.insider_networks[0].wallets == 62
    assert safety.insider_networks[0].current_holding_pct == pytest.approx(79.9, abs=0.1)


def test_top10_excludes_the_liquidity_pool():
    report = load_fixture("rug_report.json")
    safety = parse_safety(report)
    pool = safety.top_holders[0]
    assert pool.label == "AMM"
    with_pool = sum(h["pct"] for h in report["topHolders"][:10])
    assert safety.top10_holders_pct is not None
    assert safety.top10_holders_pct < with_pool


def test_rugcheck_missing_sections_stay_unknown():
    safety = parse_safety({"mint": "X"})
    assert safety.mint_authority_revoked is None
    assert safety.freeze_authority_revoked is None
    assert safety.lp_locked_pct is None
    assert safety.top10_holders_pct is None
    assert safety.dev_holding_pct is None
    assert safety.transfer_fee_pct is None


def test_rugcheck_flags_set_authorities_and_dangerous_extensions():
    report = load_fixture("rug_report.json")
    report["token"]["mintAuthority"] = "SomeAuthority"
    report["token_extensions"]["permanentDelegate"] = {"delegate": "Someone"}
    safety = parse_safety(report)
    assert safety.mint_authority_revoked is False
    assert safety.dangerous_extensions == ("permanentDelegate",)


def test_jupiter_order_parses():
    order = parse_order(load_fixture("jup_order_buy.json"))
    assert order.in_amount == 100_000_000
    assert order.out_amount > 0
    assert order.in_usd > order.out_usd > 0
    assert order.transaction is None


def test_jupiter_errors_raise():
    with pytest.raises(DataError):
        parse_order({"error": "not a valid token mint"})
    with pytest.raises(DataError):
        parse_order({**load_fixture("jup_order_buy.json"), "outAmount": "0"})
    with pytest.raises(DataError):
        parse_order({"inputMint": "a"})


def test_usd_to_lamports():
    assert usd_to_lamports(20, 80) == 250_000_000
