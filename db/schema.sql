-- =============================================================================
-- Trading System — Phase 1 Database Schema
-- Requires: TimescaleDB extension (included in timescale/timescaledb Docker image)
-- Applied automatically by Docker on first container start.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- -----------------------------------------------------------------------------
-- Instruments: Kite instrument master (NSE)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS instruments (
    instrument_token BIGINT         PRIMARY KEY,
    tradingsymbol    VARCHAR(50)    NOT NULL,
    exchange         VARCHAR(20)    NOT NULL,
    segment          VARCHAR(30)    NOT NULL DEFAULT '',
    name             VARCHAR(200)   NOT NULL DEFAULT '',
    lot_size         INTEGER        NOT NULL DEFAULT 1,
    tick_size        NUMERIC(10, 5) NOT NULL DEFAULT 0.05,
    instrument_type  VARCHAR(20)    NOT NULL DEFAULT 'EQ',
    last_synced      TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_instruments_symbol_exchange
    ON instruments (tradingsymbol, exchange);

-- -----------------------------------------------------------------------------
-- OHLCV: Historical candle data (TimescaleDB hypertable, partitioned on ts)
-- Stores raw unadjusted prices. Corporate-action adjustment is deferred.
-- Primary key includes ts (required by TimescaleDB unique constraint rule).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ohlcv (
    ts               TIMESTAMPTZ    NOT NULL,
    instrument_token BIGINT         NOT NULL REFERENCES instruments (instrument_token),
    interval         VARCHAR(10)    NOT NULL,   -- 'minute', 'day', etc.
    open             NUMERIC(15, 4) NOT NULL,
    high             NUMERIC(15, 4) NOT NULL,
    low              NUMERIC(15, 4) NOT NULL,
    close            NUMERIC(15, 4) NOT NULL,
    volume           BIGINT         NOT NULL DEFAULT 0,
    oi               BIGINT         NOT NULL DEFAULT 0,
    PRIMARY KEY (instrument_token, interval, ts)
);

SELECT create_hypertable('ohlcv', 'ts', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_ohlcv_token_interval
    ON ohlcv (instrument_token, interval, ts DESC);

-- -----------------------------------------------------------------------------
-- Features: Versioned technical indicators (long format)
-- Stored per (token, interval, feature_set_version, feature_name, ts).
-- Warmup bars produce no row (rather than NULL row) for cleanliness.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS features (
    ts                  TIMESTAMPTZ      NOT NULL,
    instrument_token    BIGINT           NOT NULL REFERENCES instruments (instrument_token),
    interval            VARCHAR(10)      NOT NULL,
    feature_set_version VARCHAR(50)      NOT NULL,
    feature_name        VARCHAR(100)     NOT NULL,
    value               DOUBLE PRECISION,
    PRIMARY KEY (instrument_token, interval, feature_set_version, feature_name, ts)
);

SELECT create_hypertable('features', 'ts', if_not_exists => TRUE);

-- -----------------------------------------------------------------------------
-- Ingestion runs: Audit log for every ingest call (resumable backfills)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id               SERIAL      PRIMARY KEY,
    instrument_token BIGINT,
    tradingsymbol    VARCHAR(50),
    interval         VARCHAR(10) NOT NULL,
    from_date        DATE        NOT NULL,
    to_date          DATE        NOT NULL,
    rows_fetched     INTEGER     NOT NULL DEFAULT 0,
    rows_stored      INTEGER     NOT NULL DEFAULT 0,
    status           VARCHAR(20) NOT NULL DEFAULT 'running',  -- running | success | failed
    error_message    TEXT,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at     TIMESTAMPTZ
);

-- -----------------------------------------------------------------------------
-- Corporate actions: Schema placeholder — not populated in Phase 1.
-- Raw unadjusted prices are the source of truth until this is implemented.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corporate_actions (
    id               SERIAL         PRIMARY KEY,
    instrument_token BIGINT,
    tradingsymbol    VARCHAR(50),
    action_type      VARCHAR(50)    NOT NULL,  -- SPLIT | BONUS | DIVIDEND
    ex_date          DATE           NOT NULL,
    ratio            NUMERIC(10, 6),
    notes            TEXT,
    created_at       TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

-- =============================================================================
-- Phase 2 — Backtesting Platform
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Backtest runs: one row per backtest, with config hash for reproducibility
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_runs (
    id               SERIAL         PRIMARY KEY,
    strategy_name    VARCHAR(100)   NOT NULL,
    strategy_version VARCHAR(50)    NOT NULL,
    params           JSONB          NOT NULL DEFAULT '{}'::jsonb,
    universe         TEXT[]         NOT NULL DEFAULT '{}',
    interval         VARCHAR(10)    NOT NULL,
    from_date        DATE           NOT NULL,
    to_date          DATE           NOT NULL,
    initial_capital  NUMERIC(18, 2) NOT NULL,
    fees_enabled     BOOLEAN        NOT NULL DEFAULT TRUE,
    slippage_bps     NUMERIC(10, 4) NOT NULL DEFAULT 0,
    config_hash      VARCHAR(64)    NOT NULL,
    metrics          JSONB          NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_backtest_runs_strategy
    ON backtest_runs (strategy_name, created_at DESC);

-- -----------------------------------------------------------------------------
-- Backtest trades: every round-trip (entry + exit) with costs and PnL
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_trades (
    id           SERIAL         PRIMARY KEY,
    run_id       INTEGER        NOT NULL REFERENCES backtest_runs (id) ON DELETE CASCADE,
    symbol       VARCHAR(50)    NOT NULL,
    side         VARCHAR(8)     NOT NULL,
    entry_ts     TIMESTAMPTZ    NOT NULL,
    entry_price  NUMERIC(15, 4) NOT NULL,
    exit_ts      TIMESTAMPTZ,
    exit_price   NUMERIC(15, 4),
    qty          INTEGER        NOT NULL,
    fees         NUMERIC(15, 4) NOT NULL DEFAULT 0,
    slippage     NUMERIC(15, 4) NOT NULL DEFAULT 0,
    pnl          NUMERIC(18, 4),
    return_pct   DOUBLE PRECISION,
    exit_reason  VARCHAR(50)
);

CREATE INDEX IF NOT EXISTS idx_backtest_trades_run
    ON backtest_trades (run_id, entry_ts);

-- -----------------------------------------------------------------------------
-- Backtest equity curve (hypertable): portfolio equity snapshot per timestamp
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_equity (
    ts              TIMESTAMPTZ      NOT NULL,
    run_id          INTEGER          NOT NULL REFERENCES backtest_runs (id) ON DELETE CASCADE,
    equity          NUMERIC(18, 4)   NOT NULL,
    cash            NUMERIC(18, 4)   NOT NULL,
    positions_value NUMERIC(18, 4)   NOT NULL,
    drawdown        DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, ts)
);

SELECT create_hypertable('backtest_equity', 'ts', if_not_exists => TRUE);

-- -----------------------------------------------------------------------------
-- Backtest signals: full audit of every strategy signal + risk decision
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_signals (
    id           SERIAL         PRIMARY KEY,
    run_id       INTEGER        NOT NULL REFERENCES backtest_runs (id) ON DELETE CASCADE,
    ts           TIMESTAMPTZ    NOT NULL,
    symbol       VARCHAR(50)    NOT NULL,
    side         VARCHAR(8)     NOT NULL,
    risk_action  VARCHAR(10)    NOT NULL,
    approved_qty INTEGER        NOT NULL DEFAULT 0,
    reason       TEXT
);

CREATE INDEX IF NOT EXISTS idx_backtest_signals_run
    ON backtest_signals (run_id, ts);

-- ===========================================================================
-- Phase 2.5: Rolling-window robustness analysis
-- ===========================================================================

-- One rolling-backtest run: a strategy swept across sliding date windows.
CREATE TABLE IF NOT EXISTS rolling_runs (
    id               SERIAL         PRIMARY KEY,
    strategy_name    VARCHAR(100)   NOT NULL,
    strategy_version VARCHAR(50)    NOT NULL,
    params           JSONB          NOT NULL DEFAULT '{}'::jsonb,
    universe         TEXT[]         NOT NULL DEFAULT '{}',
    interval         VARCHAR(10)    NOT NULL,
    from_date        DATE           NOT NULL,
    to_date          DATE           NOT NULL,
    window_days      INTEGER        NOT NULL,
    step_days        INTEGER        NOT NULL,
    summary          JSONB          NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rolling_runs_strategy
    ON rolling_runs (strategy_name, created_at DESC);

-- Per-window metrics for a rolling run.
CREATE TABLE IF NOT EXISTS rolling_windows (
    id              SERIAL           PRIMARY KEY,
    rolling_run_id  INTEGER          NOT NULL REFERENCES rolling_runs (id) ON DELETE CASCADE,
    window_start    DATE             NOT NULL,
    window_end      DATE             NOT NULL,
    metrics         JSONB            NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_rolling_windows_run
    ON rolling_windows (rolling_run_id, window_start);
