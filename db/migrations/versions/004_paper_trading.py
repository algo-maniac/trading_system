"""Paper-trading journal: signals, orders, fills, live positions (Phase 5a).

The persistent ledger the Trader agent owns. Every row traces back to the signal that
caused it (signal_id is the idempotency key end-to-end), so the knowledge-base loop can
later join closed-trade P&L back to the decision that allowed it.

Revision ID: 004
Revises: 003
Create Date: 2026-07-03
"""
from typing import Sequence, Union
from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Strategist output. id is a deterministic hash(strategy, symbol, ts, side) computed by
    # the producer — at-least-once bus redelivery upserts into a no-op instead of duplicating.
    op.execute("""
        CREATE TABLE IF NOT EXISTS live_signals (
            id            VARCHAR(64)   PRIMARY KEY,
            ts            TIMESTAMPTZ   NOT NULL,
            sleeve        VARCHAR(50)   NOT NULL,
            strategy      VARCHAR(100)  NOT NULL,
            symbol        VARCHAR(50)   NOT NULL,
            side          VARCHAR(4)    NOT NULL,
            size_hint     DOUBLE PRECISION,
            stop_pct      DOUBLE PRECISION,
            target_pct    DOUBLE PRECISION,
            trailing_stop_pct  DOUBLE PRECISION,
            trailing_atr_mult  DOUBLE PRECISION,
            reason        TEXT          NOT NULL DEFAULT '',
            features      JSONB         NOT NULL DEFAULT '{}'::jsonb,
            status        VARCHAR(20)   NOT NULL DEFAULT 'NEW',
            created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_live_signals_ts ON live_signals (ts DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_live_signals_symbol ON live_signals (symbol, ts DESC)")

    # Trader output: one order per accepted signal (paper broker fills next bar).
    op.execute("""
        CREATE TABLE IF NOT EXISTS live_orders (
            id              SERIAL        PRIMARY KEY,
            signal_id       VARCHAR(64)   NOT NULL REFERENCES live_signals (id),
            symbol          VARCHAR(50)   NOT NULL,
            side            VARCHAR(4)    NOT NULL,
            qty             INTEGER       NOT NULL,
            product         VARCHAR(10)   NOT NULL DEFAULT 'CNC',
            broker          VARCHAR(20)   NOT NULL DEFAULT 'paper',
            broker_order_id VARCHAR(100),
            status          VARCHAR(20)   NOT NULL DEFAULT 'PENDING',
            reject_reason   TEXT,
            sleeve          VARCHAR(50)   NOT NULL,
            created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_live_orders_signal ON live_orders (signal_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_live_orders_status ON live_orders (status, created_at DESC)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS live_fills (
            id          SERIAL          PRIMARY KEY,
            order_id    INTEGER         NOT NULL REFERENCES live_orders (id),
            ts          TIMESTAMPTZ     NOT NULL,
            qty         INTEGER         NOT NULL,
            price       NUMERIC(15,4)   NOT NULL,
            fees        JSONB           NOT NULL DEFAULT '{}'::jsonb,
            slippage    DOUBLE PRECISION NOT NULL DEFAULT 0
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_live_fills_order ON live_fills (order_id)")

    # The live ledger per sleeve. qty=0 rows are closed positions kept for history.
    op.execute("""
        CREATE TABLE IF NOT EXISTS live_positions (
            id             SERIAL          PRIMARY KEY,
            sleeve         VARCHAR(50)     NOT NULL,
            symbol         VARCHAR(50)     NOT NULL,
            qty            INTEGER         NOT NULL,
            avg_price      NUMERIC(15,4)   NOT NULL,
            entry_fees     DOUBLE PRECISION NOT NULL DEFAULT 0,
            entry_slippage DOUBLE PRECISION NOT NULL DEFAULT 0,
            opened_ts      TIMESTAMPTZ     NOT NULL,
            closed_ts      TIMESTAMPTZ,
            realized_pnl   DOUBLE PRECISION,
            exit_reason    TEXT,
            open_signal_id VARCHAR(64)     REFERENCES live_signals (id),
            UNIQUE (sleeve, symbol, opened_ts)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_live_positions_open ON live_positions (sleeve, symbol) WHERE closed_ts IS NULL")

    # Per-sleeve cash + equity snapshots (EOD mark-to-market job appends here).
    op.execute("""
        CREATE TABLE IF NOT EXISTS live_equity (
            id          SERIAL          PRIMARY KEY,
            ts          TIMESTAMPTZ     NOT NULL,
            sleeve      VARCHAR(50)     NOT NULL,
            cash        DOUBLE PRECISION NOT NULL,
            positions_value DOUBLE PRECISION NOT NULL,
            equity      DOUBLE PRECISION NOT NULL,
            UNIQUE (sleeve, ts)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_live_equity_sleeve ON live_equity (sleeve, ts DESC)")

    # Sleeve cash registry — the authoritative current cash per sleeve (single row each).
    op.execute("""
        CREATE TABLE IF NOT EXISTS live_sleeves (
            sleeve      VARCHAR(50)     PRIMARY KEY,
            capital     DOUBLE PRECISION NOT NULL,
            cash        DOUBLE PRECISION NOT NULL,
            updated_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS live_sleeves")
    op.execute("DROP TABLE IF EXISTS live_equity")
    op.execute("DROP TABLE IF EXISTS live_positions")
    op.execute("DROP TABLE IF EXISTS live_fills")
    op.execute("DROP TABLE IF EXISTS live_orders")
    op.execute("DROP TABLE IF EXISTS live_signals")
