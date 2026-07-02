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

## Architecture

```
CLI (trading auth | ingest | build-features | replay | verify)
  │
  ├── data/auth.py          Kite login + daily token cache (Redis)
  ├── data/kite_client.py   Rate-limited, chunked, retrying Kite wrapper
  ├── data/instruments.py   Instrument master sync + NIFTY 50 token resolution
  ├── data/ingestion.py     Orchestrates fetch → clean → upsert
  ├── data/cleaning.py      OHLC validation, dedup, gap detection vs NSE calendar
  ├── data/storage.py       TimescaleDB read/write repositories
  ├── data/raw_archive.py   Raw Parquet archive of API responses
  ├── features/             RSI, MACD, MAs, momentum, volatility, volume spikes
  └── replay/engine.py      Deterministic, point-in-time bar replay
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
| 1 — Data Foundation | **Current** | Historical ingest, features, replay |
| 2 — Backtesting | Planned | Strategy engine, Sharpe/drawdown analytics |
| 3 — Paper Trading | Planned | Live feed, simulated execution, dashboard |
| 4 — Live Trading | Planned | Zerodha execution, kill switches |
| 5 — Adaptive Intelligence | Planned | RL, regime-aware strategy switching |
| 6 — Scale | Planned | Multi-strategy portfolio, distributed infra |
