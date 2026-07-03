"""News-driven analysis + dynamic universe (Phase 5c).

news_items       — deduped StockInsights announcements (their AI tags/summaries kept)
gap_watchlist    — symbols expected to gap next session, with the news that says why
focus_list       — the day's intraday watch universe, with provenance per symbol
universe_members — news-admitted names now tradable beyond Nifty 200 (admission
                   provenance: news ref + quant gate stats + LLM verdict)

Revision ID: 005
Revises: 004
Create Date: 2026-07-03
"""
from typing import Sequence, Union
from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS news_items (
            id                SERIAL        PRIMARY KEY,
            source_link_hash  VARCHAR(64)   NOT NULL UNIQUE,
            source_link       TEXT          NOT NULL,
            nse_symbol        VARCHAR(50),
            company_name      TEXT,
            announcement_type_id INTEGER,
            announcement_type TEXT,
            sentiment         VARCHAR(20),
            summary_header    TEXT,
            summary_text      TEXT,
            published_ts      TIMESTAMPTZ,
            fetched_ts        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
            raw               JSONB         NOT NULL DEFAULT '{}'::jsonb
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_news_symbol_ts ON news_items (nse_symbol, published_ts DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_news_published ON news_items (published_ts DESC)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS gap_watchlist (
            id          SERIAL       PRIMARY KEY,
            watch_date  DATE         NOT NULL,
            symbol      VARCHAR(50)  NOT NULL,
            direction   VARCHAR(10)  NOT NULL DEFAULT 'up',
            news_id     INTEGER      REFERENCES news_items (id),
            created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            UNIQUE (watch_date, symbol)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS focus_list (
            id          SERIAL       PRIMARY KEY,
            focus_date  DATE         NOT NULL,
            symbol      VARCHAR(50)  NOT NULL,
            sources     TEXT[]       NOT NULL DEFAULT '{}',
            created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            UNIQUE (focus_date, symbol)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_focus_date ON focus_list (focus_date)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS universe_members (
            id          SERIAL       PRIMARY KEY,
            symbol      VARCHAR(50)  NOT NULL,
            admitted_ts TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            provenance  JSONB        NOT NULL DEFAULT '{}'::jsonb,
            active      BOOLEAN      NOT NULL DEFAULT TRUE
        )
    """)
    # one ACTIVE row per symbol; historical (deactivated) rows retained for attribution
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_universe_active_symbol
        ON universe_members (symbol) WHERE active
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS focus_list")
    op.execute("DROP TABLE IF EXISTS gap_watchlist")
    op.execute("DROP TABLE IF EXISTS universe_members")
    op.execute("DROP TABLE IF EXISTS news_items")
