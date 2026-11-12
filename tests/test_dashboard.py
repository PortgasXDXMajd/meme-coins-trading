"""The dashboard reads the desk's state without ever writing to it."""

from __future__ import annotations

import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from desk.agents.analyst import Analyst
from desk.agents.bear import Bear
from desk.agents.risk import RiskOfficer
from desk.dashboard.data import (
    kill_rule_counts,
    latest_scout_pulse,
    load_snapshot,
    read_heartbeat,
    realised_equity_curve,
    tail_lines,
)
from desk.debate import Debate
from desk.domain.models import Position
from desk.dossier import build_dossier
from desk.portfolio import Portfolio
from desk.storage.db import Store
from tests.conftest import (
    NOW,
    CashOnlyBroker,
    FakeLLM,
    analyst_response,
    bear_case,
    make_verdict,
    referee_decision,
)

APP = Path(__file__).parents[1] / "src" / "desk" / "dashboard" / "app.py"
SINCE = NOW - timedelta(days=1)


def make_position(**overrides) -> Position:
    defaults = {
        "mint": "Mint111111111111111111111111111111111111111",
        "symbol": "TEST",
        "pair_address": "Pair11111111111111111111111111111111111111",
        "token_amount_raw": 1_000_000,
        "cost_usd": 20.0,
        "entry_price_usd": 1.0,
        "stop_price_usd": 0.94,
        "high_water_price_usd": 1.0,
        "last_price_usd": 1.05,
        "opened_at": NOW,
    }
    return Position(**{**defaults, **overrides})


async def seed_desk(settings) -> None:
    """A desk that killed one token, vetoed another and holds one position."""
    store = Store(settings.db_path)
    store.set_float("paper_cash_usd", 230.0)
    store.record_evaluation(
        "MintA", "RUG", "filtered", {"kills": ["lp_not_burned: LP 61.8% < 99%"]}, NOW
    )
    llm = FakeLLM(bear_case(), analyst_response(), referee_decision(unrebutted=(2,)))
    referee = RiskOfficer(llm, Portfolio(CashOnlyBroker(), store), store, settings)
    debate = Debate(
        Bear(llm, settings), Analyst(llm, settings), referee, tool_factory=lambda d, ledger: []
    )
    outcome = await debate.run(build_dossier(make_verdict(), settings, NOW))
    store.save_debate(outcome, settings.log_dir / "debates" / "test.md")
    store.record_evaluation(
        "MintB", "TEST", "vetoed", {"veto_reasons": list(outcome.veto_reasons)}, NOW
    )
    store.insert_position(make_position())
    store.add_lesson("TEST", "Thin pools gap through stops.")
    store.close()

    settings.log_dir.mkdir(parents=True, exist_ok=True)
    (settings.log_dir / "desk.log").write_text(
        "2026-11-12 21:00:00 INFO    desk.agents.scout: scout: 90 discovered, 200 watched, "
        "3 in window\n"
        "2026-11-12 21:01:10 INFO    desk.agents.scout: scout: 107 discovered, 300 watched, "
        "5 in window\n"
    )


def test_scout_pulse_comes_from_the_latest_scout_line():
    lines = ["scout: 1 discovered, 2 watched, 3 in window", "filter: X killed", "nothing"]
    pulse = latest_scout_pulse(lines)
    assert pulse is not None
    assert (pulse.discovered, pulse.watched, pulse.in_window) == (1, 2, 3)
    assert latest_scout_pulse(["no scout here"]) is None


def test_tail_lines_returns_only_the_end(tmp_path):
    path = tmp_path / "desk.log"
    path.write_text("\n".join(f"line {n}" for n in range(50)))
    assert tail_lines(path, limit=2) == ["line 48", "line 49"]
    assert tail_lines(tmp_path / "missing.log", limit=2) == []


def test_heartbeat_goes_stale(tmp_path):
    path = tmp_path / "desk.log"
    path.write_text("x")
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    assert read_heartbeat(path, NOW + timedelta(seconds=30)).alive
    assert not read_heartbeat(path, NOW + timedelta(hours=1)).alive
    assert not read_heartbeat(tmp_path / "missing.log", NOW).alive


def test_equity_curve_adds_realised_pnl_in_close_order():
    later = make_position(closed_at=NOW + timedelta(hours=2), proceeds_usd=25.0)
    earlier = make_position(closed_at=NOW + timedelta(hours=1), proceeds_usd=18.0)
    curve = realised_equity_curve([later, earlier], starting_usd=250.0)
    assert [value for _, value in curve] == [248.0, 253.0]


async def test_snapshot_reflects_the_desk(settings):
    await seed_desk(settings)
    snapshot = load_snapshot(settings, SINCE, NOW)
    assert snapshot is not None
    assert snapshot.stats.equity_usd == pytest.approx(230.0 + 21.0)
    assert snapshot.stats.evaluations == {"filtered": 1, "vetoed": 1}
    assert kill_rule_counts(snapshot.evaluations) == {"lp_not_burned": 1}
    assert [d.approved for d in snapshot.debates] == [False]
    assert snapshot.scout is not None and snapshot.scout.in_window == 5
    assert len(snapshot.open_positions) == 1
    assert not snapshot.halted


def test_snapshot_is_none_before_the_desk_has_run(settings):
    assert load_snapshot(settings, SINCE, NOW) is None


async def test_read_only_store_cannot_write(settings):
    await seed_desk(settings)
    store = Store(settings.db_path, read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        store.set_float("paper_cash_usd", 0.0)
    store.close()


async def test_dashboard_page_renders(settings, monkeypatch):
    await seed_desk(settings)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("LOG_DIR", str(settings.log_dir))

    page = AppTest.from_file(str(APP), default_timeout=30).run()

    assert not page.exception
    labels = {metric.label: metric.value for metric in page.metric}
    assert labels["Equity"] == "$251.00"
    assert labels["3 · Bear vs Analyst: debated"] == "1"
    assert any("vetoed" in error.value for error in page.error)
