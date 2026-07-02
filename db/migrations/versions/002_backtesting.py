"""Backtesting tables: backtest_runs, backtest_trades, backtest_equity, backtest_signals

Revision ID: 002
Revises: 001
Create Date: 2026-06-28
"""
from typing import Sequence, Union
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
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
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_backtest_runs_strategy ON backtest_runs (strategy_name, created_at DESC)")

    op.execute("""
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
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_backtest_trades_run ON backtest_trades (run_id, entry_ts)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS backtest_equity (
            ts              TIMESTAMPTZ    NOT NULL,
            run_id          INTEGER        NOT NULL REFERENCES backtest_runs (id) ON DELETE CASCADE,
            equity          NUMERIC(18, 4) NOT NULL,
            cash            NUMERIC(18, 4) NOT NULL,
            positions_value NUMERIC(18, 4) NOT NULL,
            drawdown        DOUBLE PRECISION NOT NULL DEFAULT 0,
            PRIMARY KEY (run_id, ts)
        )
    """)
    op.execute("SELECT create_hypertable('backtest_equity', 'ts', if_not_exists => TRUE)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS backtest_signals (
            id           SERIAL         PRIMARY KEY,
            run_id       INTEGER        NOT NULL REFERENCES backtest_runs (id) ON DELETE CASCADE,
            ts           TIMESTAMPTZ    NOT NULL,
            symbol       VARCHAR(50)    NOT NULL,
            side         VARCHAR(8)     NOT NULL,
            risk_action  VARCHAR(10)    NOT NULL,
            approved_qty INTEGER        NOT NULL DEFAULT 0,
            reason       TEXT
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_backtest_signals_run ON backtest_signals (run_id, ts)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS backtest_signals")
    op.execute("DROP TABLE IF EXISTS backtest_equity")
    op.execute("DROP TABLE IF EXISTS backtest_trades")
    op.execute("DROP TABLE IF EXISTS backtest_runs")
