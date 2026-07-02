"""Initial schema: instruments, ohlcv, features, ingestion_runs, corporate_actions

Revision ID: 001
Revises:
Create Date: 2026-06-27
"""
from typing import Sequence, Union
from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE")

    op.execute("""
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
        )
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_instruments_symbol_exchange
            ON instruments (tradingsymbol, exchange)
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS ohlcv (
            ts               TIMESTAMPTZ    NOT NULL,
            instrument_token BIGINT         NOT NULL REFERENCES instruments (instrument_token),
            interval         VARCHAR(10)    NOT NULL,
            open             NUMERIC(15, 4) NOT NULL,
            high             NUMERIC(15, 4) NOT NULL,
            low              NUMERIC(15, 4) NOT NULL,
            close            NUMERIC(15, 4) NOT NULL,
            volume           BIGINT         NOT NULL DEFAULT 0,
            oi               BIGINT         NOT NULL DEFAULT 0,
            PRIMARY KEY (instrument_token, interval, ts)
        )
    """)

    op.execute("SELECT create_hypertable('ohlcv', 'ts', if_not_exists => TRUE)")

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_ohlcv_token_interval
            ON ohlcv (instrument_token, interval, ts DESC)
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS features (
            ts                  TIMESTAMPTZ      NOT NULL,
            instrument_token    BIGINT           NOT NULL REFERENCES instruments (instrument_token),
            interval            VARCHAR(10)      NOT NULL,
            feature_set_version VARCHAR(50)      NOT NULL,
            feature_name        VARCHAR(100)     NOT NULL,
            value               DOUBLE PRECISION,
            PRIMARY KEY (instrument_token, interval, feature_set_version, feature_name, ts)
        )
    """)

    op.execute("SELECT create_hypertable('features', 'ts', if_not_exists => TRUE)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS ingestion_runs (
            id               SERIAL      PRIMARY KEY,
            instrument_token BIGINT,
            tradingsymbol    VARCHAR(50),
            interval         VARCHAR(10) NOT NULL,
            from_date        DATE        NOT NULL,
            to_date          DATE        NOT NULL,
            rows_fetched     INTEGER     NOT NULL DEFAULT 0,
            rows_stored      INTEGER     NOT NULL DEFAULT 0,
            status           VARCHAR(20) NOT NULL DEFAULT 'running',
            error_message    TEXT,
            started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at     TIMESTAMPTZ
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS corporate_actions (
            id               SERIAL         PRIMARY KEY,
            instrument_token BIGINT,
            tradingsymbol    VARCHAR(50),
            action_type      VARCHAR(50)    NOT NULL,
            ex_date          DATE           NOT NULL,
            ratio            NUMERIC(10, 6),
            notes            TEXT,
            created_at       TIMESTAMPTZ    NOT NULL DEFAULT NOW()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS corporate_actions")
    op.execute("DROP TABLE IF EXISTS ingestion_runs")
    op.execute("DROP TABLE IF EXISTS features")
    op.execute("DROP TABLE IF EXISTS ohlcv")
    op.execute("DROP INDEX IF EXISTS idx_instruments_symbol_exchange")
    op.execute("DROP TABLE IF EXISTS instruments")
