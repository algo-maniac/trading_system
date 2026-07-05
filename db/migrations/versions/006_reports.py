"""Reporter agent output (Phase 5d').

reports — one row per generated period report (daily/weekly/monthly), keyed by
(period_type, period_start) so data-driven generation is idempotent. The full
computed payload is kept as JSONB for querying; the rendered markdown is stored
verbatim (the same text written to reports/{namespace}/... on disk).

Revision ID: 006
Revises: 005
Create Date: 2026-07-05
"""
from typing import Sequence, Union

from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id           BIGSERIAL    PRIMARY KEY,
            period_type  TEXT         NOT NULL CHECK (period_type IN ('daily','weekly','monthly')),
            period_start DATE         NOT NULL,
            period_end   DATE         NOT NULL,
            payload      JSONB        NOT NULL,
            markdown     TEXT         NOT NULL,
            created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            UNIQUE (period_type, period_start)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS reports")
