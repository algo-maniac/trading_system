"""Rolling-window robustness tables: rolling_runs, rolling_windows

Revision ID: 003
Revises: 002
Create Date: 2026-06-28
"""
from typing import Sequence, Union
from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
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
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_rolling_runs_strategy ON rolling_runs (strategy_name, created_at DESC)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS rolling_windows (
            id              SERIAL           PRIMARY KEY,
            rolling_run_id  INTEGER          NOT NULL REFERENCES rolling_runs (id) ON DELETE CASCADE,
            window_start    DATE             NOT NULL,
            window_end      DATE             NOT NULL,
            metrics         JSONB            NOT NULL DEFAULT '{}'::jsonb
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_rolling_windows_run ON rolling_windows (rolling_run_id, window_start)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS rolling_windows")
    op.execute("DROP TABLE IF EXISTS rolling_runs")
