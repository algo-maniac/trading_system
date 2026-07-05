# Trading System — Phase 1: Data Foundation

Agentic AI-powered algorithmic trading platform. This phase delivers reliable, deterministic
data infrastructure for the NIFTY 50: historical ingestion from Zerodha Kite Connect,
cleaning, feature computation, and a point-in-time replay engine.

## Quick start

### 1. Prerequisites

- Python 3.11+
- Docker + Docker Compose
- Zerodha Kite Connect app with **Historical Data API add-on** enabled

### 2. Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. Configure credentials

```bash
cp .env.example .env
# Edit .env — set KITE_API_KEY and KITE_API_SECRET
```

> ⚠️ Never commit `.env`. It is gitignored.

### 4. Start infrastructure

```bash
docker compose up -d
# TimescaleDB applies db/schema.sql automatically on first start
```

### 5. Authenticate with Kite (run every trading day)

```bash
trading auth
# Follow the printed URL → log in → paste the redirect URL back
```

### 6. Sync instrument master

```bash
trading sync-instruments
```

### 7. Ingest historical data

```bash
# Example: daily bars for all NIFTY 50 symbols over 1 year
trading ingest --interval day --from 2023-01-01 --to 2023-12-31

# Specific symbols + minute bars (slower, requires paid historical add-on)
trading ingest -s RELIANCE -s TCS --interval minute --from 2024-01-01 --to 2024-03-31
```

### 8. Build features

```bash
trading build-features --interval day --from 2023-01-01 --to 2023-12-31
```

### 9. Replay and verify

```bash
trading replay --interval day --from 2023-01-01 --to 2023-12-31 --limit 30
trading verify --interval day --from 2023-01-01 --to 2023-12-31
```

### Run tests

```bash
pytest -v
```

## Live paper-trading stack (Phase 5)

```bash
docker-compose up -d               # timescaledb, redis, reader(s), analyst,
                                   # strategist, trader, reporter
trading auth                       # every morning (~before 08:30 IST)
trading report --period daily --date 2026-07-04 [--force]   # on-demand report
```

Reports also generate automatically (data-driven: a period is written once the next
period's data exists) into `reports/live/{daily,weekly,monthly}/` and the `reports`
DB table. LLM admission reviews use Claude Haiku (`LLM_PROVIDER=anthropic` in .env),
hard-capped at `LLM_MONTHLY_USD_CAP` per month.

## Replay harness (test the whole pipeline on historic data)

Runs the SAME strategist/trader/reporter binaries against a `trading_replay` DB
clone + Redis DB 1 — fully isolated from the live paper book, months replay in
minutes. Consumers must start BEFORE the replayer (consumer groups begin at the
stream tail):

```bash
scripts/replay_reset.sh                                        # rebuild replay DB
docker-compose -f docker-compose.replay.yml up -d strategist trader reporter
docker-compose -f docker-compose.replay.yml run --rm replayer \
    --from 2025-01-01 --to 2025-04-03 --intervals day          # ~5 min for a quarter
# optional: --intervals day,5minute · --with-news (StockInsights, MockLLM)
```

Replay reports land in `reports/replay/…`; inspect the ledger with
`psql -d trading_replay` (`live_orders`, `live_positions`, `live_equity`).

## Architecture

```
CLI (trading auth | sync-instruments | ingest | build-features | replay | verify
     | backtest | backtest-book | backtest-multi | backtest-sweep | backtest-rolling | report)
  │
  ├── data/            Kite ingest → clean → store; news feed (StockInsights)
  │   ├── auth.py            Kite login + daily token cache (Redis)
  │   ├── kite_client.py     Rate-limited, chunked, retrying Kite wrapper
  │   ├── instruments.py     Instrument master sync + NIFTY 50 token resolution
  │   ├── ingestion.py       Orchestrates fetch → clean → upsert (feature-integrated, gap-aware)
  │   ├── cleaning.py        OHLC validation, dedup, gap detection vs NSE calendar
  │   ├── storage.py         TimescaleDB read/write repositories
  │   ├── raw_archive.py     Raw Parquet archive of API responses
  │   ├── screener.py        Volatile mid-cap universe screen
  │   └── stockinsights_client.py · news_store.py   News fetch + storage
  ├── features/        RSI, MACD, MAs, momentum, volatility, volume spikes
  ├── replay/engine.py Deterministic, point-in-time bar replay
  ├── strategy/        9 strategies: momentum, cross-sectional, mean-reversion, VWAP-reversion,
  │                    ORB, swing pullback/breakout (+ registry, multi-strategy book)
  ├── backtest/        Event-driven backtest engine + result persistence
  ├── analytics/       Sharpe/drawdown metrics, book allocation, walk-forward, sweeps, reports
  ├── risk/            Position sizing, exposure caps, stop rules
  ├── portfolio/       Backtest + live portfolio state
  ├── execution/       Paper broker, fee model, Postgres ledger (live_orders/positions/equity)
  ├── llm/             LLM abstraction + Anthropic Haiku client (+ mock)
  ├── bus/             Redis Streams message bus (serde, signals)
  └── agents/          Long-running services over the bus:
      ├── reader · reader-live   Historical + live WebSocket data plane
      ├── analyst                News-driven market analysis + dynamic universe
      ├── strategist             Runs the strategy book, emits trade signals
      ├── trader                 LLM-reviewed admission → paper broker → ledger
      ├── reporter               Daily/weekly/monthly performance reports
      ├── backtester             Forward / paper-test harness
      └── replayer               Feeds historic bars for the replay harness
```

## Kite Connect limitations

| Constraint | Impact |
|---|---|
| Access token expires daily | `trading auth` required each morning |
| Historical Data API is a paid add-on | Ingestion fails without the subscription |
| Minute data: max ~60 days per request | Client chunks automatically |
| ~3 historical requests/sec rate limit | Client throttles automatically |
| No news API | News Intelligence Agent is Phase 2+ |
| No corporate actions data | Price adjustment deferred; raw unadjusted prices stored |

## Phase roadmap

| Phase | Status | Description |
|---|---|---|
| 1 — Data Foundation | ✅ Done | Historical ingest, cleaning, features, replay |
| 2 — Backtesting | ✅ Done | Event-driven engine, Sharpe/drawdown analytics, risk & execution models |
| 3 — Strategy R&D | ✅ Done | 9 strategies, cross-sectional momentum, rolling-window & walk-forward validation, locked book |
| 4 — Agentic Platform | ✅ Done | Redis-Streams bus, reader/backtester agents, intraday + swing strategies |
| 5 — Live Paper Trading | ✅ Done | Live WebSocket data plane, analyst/strategist/trader/reporter agents, LLM admission reviews, replay harness |
| 6 — Live Trading | Planned | Zerodha order execution, kill switches |
| 7 — Adaptive Intelligence | Planned | Regime-aware strategy switching, RL |
