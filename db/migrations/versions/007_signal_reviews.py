"""Shadow LLM signal reviews (Phase 5d).

signal_reviews — one verdict per BUY SignalEvent, keyed by the signal's deterministic
id so a redelivered signal is reviewed once. Shadow-only: the trader never reads this
table; analytics/review_eval.py joins it with forward prices to score the reviewer.
`error` non-null marks a fail-open verdict (reviewer unavailable), excluded from evals.

Revision ID: 007
Revises: 006
Create Date: 2026-10-30
"""
from typing import Sequence, Union

from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS signal_reviews (
            signal_id   TEXT          PRIMARY KEY,
            ts          TIMESTAMPTZ   NOT NULL,
            sleeve      TEXT          NOT NULL,
            strategy    TEXT          NOT NULL,
            symbol      VARCHAR(50)   NOT NULL,
            side        VARCHAR(4)    NOT NULL,
            ref_price   DOUBLE PRECISION NOT NULL,
            approve     BOOLEAN       NOT NULL,
            confidence  DOUBLE PRECISION NOT NULL,
            reasoning   TEXT          NOT NULL,
            tools_used  TEXT[]        NOT NULL DEFAULT '{}',
            cost_usd    DOUBLE PRECISION NOT NULL DEFAULT 0,
            reviewer    TEXT          NOT NULL,
            error       TEXT,
            created_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_signal_reviews_ts ON signal_reviews (ts)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS signal_reviews")
