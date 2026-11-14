# Memecoin Desk

A six-agent Solana memecoin trading desk with a devil's advocate. Every buy must survive a debate: the Bear gives three on-chain reasons not to buy, the Analyst must rebut each with data, and Risk rules on each one. Any reason left unrebutted is a veto.

It runs in **paper mode** by default: live market data and live Jupiter quotes, simulated fills.

## The pipeline

| Agent | How | What it does |
|---|---|---|
| Scout | code | Finds pairs 30 min – 72 h old with a $300k – $20M market cap |
| Filter | code | Kills on: mint or freeze authority not revoked, LP not burned, liquidity < $40k, top 10 > 25%, dev > 3%, transfer fee, dangerous token extensions, no sell route. Missing data is a kill |
| Bear | Claude | Three concrete on-chain reasons not to buy, each citing dossier facts. Can inspect wallets, quote large exits and refresh the market |
| Analyst | Claude | Rebuts each reason with cited data, or concedes |
| Risk | Claude + code | Referees the debate (doubt goes to the Bear), sets the −6% stop before entry, then trails the stop 6% below the high once +12% is reached |
| Executor | code | Buys at most 8% of equity, and only with Risk's approval |

Hard limits live in code (`src/desk/domain/rules.py`) and cannot be argued with. Claude does the judgement. After each closed trade Claude also writes a one-line lesson that the Bear reads in later debates.

## Run it

```bash
docker compose up -d --build     # start the desk and the dashboard (both restart themselves)
docker compose logs -f desk      # watch
docker compose run --rm desk report --ai   # P&L summary plus a Claude-written report
docker compose down              # stop; open positions are saved and resumed
```

The dashboard is at http://localhost:8501. Re-run `docker compose up -d --build` after changing code or `.env`.

Docker does not keep the Mac awake: plug it in and leave `caffeinate -ims` running in a terminal. Times in logs and the dashboard use `TZ` (default `Europe/Berlin`; set `TZ=` in `.env` to change it).

Without Docker:

```bash
nohup scripts/run_overnight.sh >/dev/null 2>&1 &   # keeps the Mac awake, restarts on crash
tail -f logs/overnight.out                         # watch
pkill -TERM -f "desk run"                          # stop cleanly
uv run desk report --ai                            # summary
uv run desk once --no-debate                       # one Scout + Filter pass, no Claude calls
```

Run the desk either in Docker or natively, not both at once: they share `data/desk.db`.

## Dashboard

```bash
uv run desk ui        # then open http://localhost:8501
```

A read-only Streamlit page that refreshes itself: equity and cash, where tokens stop in the pipeline, every Filter kill and veto with its reason, each debate side by side (Bear, Analyst, Risk ruling), positions, lessons and the live log. It only reads `data/desk.db` and `logs/`, so it is safe to run beside the desk. With Docker it starts as the `dashboard` service on the same address.

To stop new entries without stopping the desk, create `data/HALT`. Open positions stay managed.

## Where to look

- `logs/debates/<date>/<time>-<symbol>-<mint>.md` — the full debate for every token that passed the Filter: dossier, three reasons, three rebuttals, three rulings, evidence gathered.
- `logs/desk.log` — everything the desk did, in order.
- `logs/events.jsonl` — the same as structured records (kills, debates, buys, sells).
- `data/desk.db` — SQLite: positions, debates, lessons, paper cash, and `price_checks` (every stop-monitor pass: the Jupiter quote price, the DexScreener price, which one was used, the stop and the high).

## Settings

Every threshold is in `src/desk/config.py` and can be overridden in `.env` (see `.env.example`). Defaults: stop −6%, trail arms at +12% and follows 6% below the high, 8% per trade, 5 open positions, new entries halt after a 20% daily loss, 20 debates per hour.

Optional free keys that help: `JUPITER_API_KEY` (doubles the quote rate limit) and `HELIUS_API_KEY` (faster RPC for wallet lookups).

## Going live

Live trading is built but has not been run with real funds. Do these in order:

1. Review several nights of paper results and debate transcripts.
2. `uv run python scripts/new_wallet.py` creates a dedicated wallet in `secrets/wallet.json`. Back the file up.
3. Send that address a small amount of SOL from an exchange. Only what you can lose.
4. Add `JUPITER_API_KEY` to `.env`, set `TRADING_MODE=live`, and watch the first trades.

Things to know before risking money:

- Stops are market sells triggered by a 5-second poll. Each poll prices the position from a live Jupiter sell quote (DexScreener can lag a dump) and falls back to DexScreener when no quote comes back. A dump faster than one poll still fills below −6%.
- A round trip costs roughly 1–4% on these tokens (pool fees, Jupiter's fee, price impact). Paper P&L already includes it because fills use real quotes.
- The wallet key sits unencrypted on this machine. Keep only the trading bankroll in it.

## Development

```bash
uv sync
uv run pytest        # no network, no API calls
uv run ruff check .
```

Layout: `agents/` (the six agents), `domain/` (typed handoffs and pure rules), `data/` (API adapters), `brokers/` (paper and live), `debate.py`, `pipeline.py` (the only place agents are called), `dashboard/` (Streamlit page and its read-only queries), `main.py` (wiring and CLI).
