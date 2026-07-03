"""Rolling volatile-universe screen (Phase 5a0).

Selects the intraday gap-ORB universe from the Nifty-200 pool by *rule*, not by hand:
the top-N names by average intraday range over a trailing window, filtered to a minimum
median daily turnover so the 5-bps-class slippage model stays defensible. Re-run at each
month boundary (or any as-of date) — a hand-picked static list bakes in selection bias;
this screen is what gets paper-traded and what backtests must use for any period.

Split into a pure ranking function (`rank_by_range`, unit-testable on plain rows) and a
thin DB reader (`volatile_universe`) that feeds it.
"""
from dataclasses import dataclass
from datetime import date, timedelta

import psycopg

from trading_system.observability import get_logger

logger = get_logger(__name__)

_CRORE = 1e7


@dataclass(frozen=True)
class ScreenRow:
    symbol: str
    avg_range_pct: float      # mean (high-low)/close over the window
    median_turnover: float    # median close*volume (₹) over the window
    sessions: int             # bars observed in the window


def rank_by_range(
    rows: list[ScreenRow],
    *,
    top_n: int = 20,
    min_turnover_cr: float = 50.0,
    min_sessions: int = 40,
) -> list[str]:
    """Rank screen rows by avg intraday range, keeping only liquid, well-observed names.

    - `min_turnover_cr` guards the slippage model (thin names trade worse than modeled).
    - `min_sessions` drops names with too little history in the window (fresh listings,
      long suspensions) whose range average is noise.
    Deterministic: ties broken by symbol.
    """
    eligible = [
        r for r in rows
        if r.median_turnover >= min_turnover_cr * _CRORE and r.sessions >= min_sessions
    ]
    eligible.sort(key=lambda r: (-r.avg_range_pct, r.symbol))
    return [r.symbol for r in eligible[:top_n]]


def volatile_universe(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    pool: list[str],
    as_of: date,
    *,
    top_n: int = 20,
    lookback_days: int = 90,
    min_turnover_cr: float = 50.0,
    min_sessions: int = 40,
) -> list[str]:
    """The gap-ORB universe as of `as_of`: screen `pool` over the trailing window.

    Uses only data strictly BEFORE `as_of` (no lookahead — safe to call at 09:00 on the
    as-of day itself).
    """
    since = as_of - timedelta(days=lookback_days)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.tradingsymbol,
                   avg((o.high - o.low) / nullif(o.close, 0))            AS avg_range_pct,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY o.close * o.volume)
                       AS median_turnover,
                   count(*)                                              AS sessions
            FROM ohlcv o
            JOIN instruments i USING (instrument_token)
            WHERE o.interval = 'day'
              AND i.tradingsymbol = ANY(%s)
              AND o.ts >= %s AND o.ts < %s
            GROUP BY i.tradingsymbol
            """,
            (pool, since, as_of),
        )
        rows = [
            ScreenRow(symbol=r[0], avg_range_pct=float(r[1] or 0.0),
                      median_turnover=float(r[2] or 0.0), sessions=int(r[3]))
            for r in cur.fetchall()
        ]
    selected = rank_by_range(
        rows, top_n=top_n, min_turnover_cr=min_turnover_cr, min_sessions=min_sessions
    )
    logger.info(
        "volatile_screen", as_of=str(as_of), pool=len(pool), eligible_rows=len(rows),
        selected=len(selected),
    )
    return selected
