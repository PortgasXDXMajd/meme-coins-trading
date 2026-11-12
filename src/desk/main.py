"""Entry point: wires the desk together and runs it.

desk run                 run until stopped (discovery loop + risk loop)
desk once [--no-debate]  one discovery pass, then exit
desk report [--ai]       summary of the last N hours
desk ui                  read-only Streamlit dashboard
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path

import anthropic
import httpx

from desk.agents.analyst import Analyst
from desk.agents.bear import Bear
from desk.agents.executor import Executor
from desk.agents.filter import Filter
from desk.agents.risk import RiskOfficer, StopMonitor
from desk.agents.scout import Scout
from desk.brokers.base import Broker
from desk.brokers.jupiter_live import JupiterLiveBroker, load_keypair
from desk.brokers.paper import PaperBroker
from desk.config import Settings
from desk.data.dexscreener import DexScreener
from desk.data.geckoterminal import GeckoTerminal
from desk.data.http import DataError
from desk.data.jupiter import Jupiter
from desk.data.rugcheck import RugCheck
from desk.data.solana_rpc import SolanaRpc, rpc_url
from desk.debate import Debate
from desk.evidence import build_evidence_tools
from desk.llm.client import ClaudeLLM
from desk.pipeline import Pipeline
from desk.portfolio import Portfolio
from desk.postmortem import PostMortem
from desk.report import default_since, render_text, write_ai_report
from desk.storage.db import Store
from desk.storage.debate_log import EventLog

log = logging.getLogger("desk")

HTTP_TIMEOUT_SECONDS = 20
LLM_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class Desk:
    settings: Settings
    store: Store
    llm: ClaudeLLM
    pipeline: Pipeline
    monitor: StopMonitor


def configure_logging(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(settings.log_dir / "desk.log", encoding="utf-8"),
        ],
        force=True,
    )
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _secret(value: object) -> str | None:
    return value.get_secret_value() if value is not None else None  # type: ignore[attr-defined]


def build_broker(settings: Settings, jupiter: Jupiter, rpc: SolanaRpc, store: Store) -> Broker:
    if not settings.is_live:
        return PaperBroker(jupiter, store, settings)
    broker = JupiterLiveBroker(jupiter, rpc, load_keypair(settings.wallet_keyfile))
    log.warning("LIVE TRADING with wallet %s. Real funds are at risk.", broker.address)
    return broker


@contextlib.asynccontextmanager
async def build_desk(settings: Settings) -> AsyncGenerator[Desk]:
    """Composition root: every dependency is created and connected here."""
    async with (
        httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as http,
        anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(), timeout=LLM_TIMEOUT_SECONDS
        ) as claude,
    ):
        store = Store(settings.db_path)
        events = EventLog(settings.log_dir)
        dex = DexScreener(http)
        gecko = GeckoTerminal(http)
        rugcheck = RugCheck(http)
        jupiter = Jupiter(http, _secret(settings.jupiter_api_key))
        rpc = SolanaRpc(http, rpc_url(_secret(settings.helius_api_key)))
        llm = ClaudeLLM(
            claude,
            model=settings.debate_model,
            effort=settings.llm_effort,
            max_tool_rounds=settings.max_evidence_tool_rounds,
        )

        broker = build_broker(settings, jupiter, rpc, store)
        portfolio = Portfolio(broker, store)
        executor = Executor(broker, dex, portfolio, store, settings, events)
        risk = RiskOfficer(llm, portfolio, store, settings)
        debate = Debate(
            Bear(llm, settings),
            Analyst(llm, settings),
            risk,
            tool_factory=lambda dossier, ledger: build_evidence_tools(
                dossier, ledger, dex, jupiter, rpc
            ),
            lessons=lambda: store.recent_lessons(settings.lessons_in_prompt),
        )
        pipeline = Pipeline(
            Scout(dex, gecko, settings),
            Filter(rugcheck, jupiter, settings),
            debate,
            risk,
            executor,
            store,
            settings,
            events,
        )
        monitor = StopMonitor(
            dex, jupiter, executor, store, settings, events, on_close=PostMortem(llm, store).review
        )
        try:
            yield Desk(settings, store, llm, pipeline, monitor)
        finally:
            store.close()


async def _loop(name: str, interval: float, step, stop: asyncio.Event) -> None:
    """Run `step` forever. One failed pass is logged and never ends the loop."""
    while not stop.is_set():
        try:
            await step()
        except DataError as exc:
            log.warning("%s: data source failed, will retry: %s", name, exc)
        except Exception:
            log.exception("%s: unexpected error, will retry", name)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


async def run_forever(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)

    async with build_desk(settings) as desk:
        open_count = len(desk.store.open_positions())
        log.info(
            "desk started: %s mode, model %s, %d open position(s) resumed",
            settings.trading_mode,
            settings.debate_model,
            open_count,
        )
        # Risk runs on its own loop so a slow debate can never delay a stop.
        await asyncio.gather(
            _loop("risk", settings.risk_interval_seconds, desk.monitor.check_positions, stop),
            _loop("discovery", settings.scout_interval_seconds, desk.pipeline.run_cycle, stop),
        )
        log.info("desk stopped; %d position(s) left open", len(desk.store.open_positions()))


async def run_once(settings: Settings, *, debate: bool) -> None:
    async with build_desk(settings) as desk:
        await desk.monitor.check_positions()
        opened = await desk.pipeline.run_cycle(debate=debate)
        log.info("single pass done: %d position(s) opened", len(opened))


async def run_report(settings: Settings, *, hours: float, ai: bool) -> None:
    since = default_since(hours)
    async with build_desk(settings) as desk:
        print(render_text(desk.store, settings, since))
        if ai:
            path = await write_ai_report(desk.llm, desk.store, settings, since)
            print(f"\nAI report written to {path}\n")
            print(path.read_text(encoding="utf-8"))


def run_dashboard(host: str, port: int) -> None:
    """Replace this process with Streamlit serving the dashboard."""
    app = Path(__file__).parent / "dashboard" / "app.py"
    os.execv(
        sys.executable,
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(app),
            f"--server.address={host}",
            f"--server.port={port}",
            "--server.headless=true",
            "--browser.gatherUsageStats=false",
        ],
    )


def cli() -> None:
    parser = argparse.ArgumentParser(prog="desk", description="Solana memecoin desk")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("run", help="run until stopped")
    once = commands.add_parser("once", help="one discovery pass")
    once.add_argument("--no-debate", action="store_true", help="stop after Scout and Filter")
    report = commands.add_parser("report", help="summarise recent activity")
    report.add_argument("--hours", type=float, default=24)
    report.add_argument("--ai", action="store_true", help="add a Claude-written report")
    ui = commands.add_parser("ui", help="read-only dashboard in the browser")
    ui.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 inside Docker")
    ui.add_argument("--port", type=int, default=8501)
    args = parser.parse_args()

    if args.command == "ui":
        run_dashboard(args.host, args.port)

    settings = Settings()  # type: ignore[call-arg]  # values come from the environment
    configure_logging(settings)
    if args.command == "once":
        asyncio.run(run_once(settings, debate=not args.no_debate))
    elif args.command == "report":
        asyncio.run(run_report(settings, hours=args.hours, ai=args.ai))
    else:
        asyncio.run(run_forever(settings))


if __name__ == "__main__":
    cli()
