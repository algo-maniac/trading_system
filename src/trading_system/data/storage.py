"""TimescaleDB repositories: read/write for instruments, ohlcv, features, audit."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Generator, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg
from psycopg.rows import dict_row

from trading_system.config import get_settings
from trading_system.observability import get_logger

logger = get_logger(__name__)

_UTC = timezone.utc
_IST = ZoneInfo("Asia/Kolkata")


def _ist_day_bounds(from_date: date, to_date: date) -> tuple[datetime, datetime]:
    """UTC-aware [from_dt, to_dt) bracketing the IST calendar days from_date..to_date.

    NSE day-interval bars are stored at IST midnight of the trading session, which is
    18:30 UTC of the PREVIOUS calendar date (IST = UTC+5:30) — e.g. the 2026-07-03
    session's day-bar has ts=2026-07-02T18:30:00+00. A boundary built from raw UTC
    midnight (`datetime(y,m,d, tzinfo=UTC)`) misses that row entirely for any
    single-day window (`from_date == to_date`), which is invisible in wide multi-year
    backtest ranges but returns zero rows for a live "give me today's bar" query —
    exactly the bug that silently zeroed the EOD reader's publish count.
    Intraday bars (always within the same UTC calendar date as their IST date, since
    NSE hours 09:15-15:30 IST = 03:45-10:00 UTC never cross a UTC midnight) are
    unaffected by anchoring the boundary this way instead — the window is simply
    shifted 5.5h earlier on both ends, which still safely brackets them.
    """
    from_dt = datetime(from_date.year, from_date.month, from_date.day, tzinfo=_IST).astimezone(_UTC)
    to_dt = datetime(
        (to_date + timedelta(days=1)).year,
        (to_date + timedelta(days=1)).month,
        (to_date + timedelta(days=1)).day,
        tzinfo=_IST,
    ).astimezone(_UTC)
    return from_dt, to_dt


@contextmanager
def get_connection() -> Generator[psycopg.Connection, None, None]:  # type: ignore[type-arg]
    with psycopg.connect(get_settings().database_url) as conn:
        yield conn


# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------

def upsert_instruments(conn: psycopg.Connection, instruments: list[dict]) -> int:  # type: ignore[type-arg]
    if not instruments:
        return 0
    sql = """
        INSERT INTO instruments (
            instrument_token, tradingsymbol, exchange, segment,
            name, lot_size, tick_size, instrument_type, last_synced
        ) VALUES (
            %(instrument_token)s, %(tradingsymbol)s, %(exchange)s, %(segment)s,
            %(name)s, %(lot_size)s, %(tick_size)s, %(instrument_type)s, NOW()
        )
        ON CONFLICT (instrument_token) DO UPDATE SET
            tradingsymbol   = EXCLUDED.tradingsymbol,
            exchange        = EXCLUDED.exchange,
            segment         = EXCLUDED.segment,
            name            = EXCLUDED.name,
            lot_size        = EXCLUDED.lot_size,
            tick_size       = EXCLUDED.tick_size,
            instrument_type = EXCLUDED.instrument_type,
            last_synced     = NOW()
    """
    with conn.cursor() as cur:
        cur.executemany(sql, instruments)
    conn.commit()
    return len(instruments)


def get_symbol_token_map(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    symbols: list[str],
    exchange: str = "NSE",
) -> dict[str, int]:
    if not symbols:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tradingsymbol, instrument_token FROM instruments "
            "WHERE tradingsymbol = ANY(%s) AND exchange = %s",
            (symbols, exchange),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# OHLCV
# ---------------------------------------------------------------------------

def upsert_ohlcv(conn: psycopg.Connection, df: pd.DataFrame) -> int:  # type: ignore[type-arg]
    if df.empty:
        return 0
    cols = ["ts", "instrument_token", "interval", "open", "high", "low", "close", "volume"]
    records = df[cols].to_dict("records")
    # RETURNING ts lets us count only the rows actually inserted (not the ON CONFLICT skips).
    sql = """
        INSERT INTO ohlcv (ts, instrument_token, interval, open, high, low, close, volume)
        VALUES (%(ts)s, %(instrument_token)s, %(interval)s,
                %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s)
        ON CONFLICT (instrument_token, interval, ts) DO NOTHING
        RETURNING ts
    """
    inserted = 0
    with conn.cursor() as cur:
        cur.executemany(sql, records, returning=True)
        while True:
            if cur.fetchone() is not None:
                inserted += 1
            if not cur.nextset():
                break
    conn.commit()
    return inserted


def read_ohlcv(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    instrument_token: int,
    interval: str,
    from_date: date,
    to_date: date,
) -> pd.DataFrame:
    from_dt, to_dt = _ist_day_bounds(from_date, to_date)
    sql = """
        SELECT ts, instrument_token, interval, open, high, low, close, volume
        FROM ohlcv
        WHERE instrument_token = %s AND interval = %s AND ts >= %s AND ts < %s
        ORDER BY ts
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (instrument_token, interval, from_dt, to_dt))
        rows = cur.fetchall()
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def get_ohlcv_bar_counts(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    instrument_token: int,
    interval: str,
    from_date: date,
    to_date: date,
) -> dict[date, int]:
    """Bars already stored, grouped by trading date — used by ingestion gap-filling.

    NSE sessions (09:15–15:30 IST → 03:45–10:00 UTC) never cross UTC midnight, so the
    UTC date of a bar equals its trading-session date; grouping by it needs no tz math.
    """
    from_dt = datetime(from_date.year, from_date.month, from_date.day, tzinfo=_UTC)
    to_dt = datetime(
        (to_date + timedelta(days=1)).year,
        (to_date + timedelta(days=1)).month,
        (to_date + timedelta(days=1)).day,
        tzinfo=_UTC,
    )
    sql = """
        SELECT (ts AT TIME ZONE 'UTC')::date AS d, COUNT(*) AS n
        FROM ohlcv
        WHERE instrument_token = %s AND interval = %s AND ts >= %s AND ts < %s
        GROUP BY (ts AT TIME ZONE 'UTC')::date
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (instrument_token, interval, from_dt, to_dt))
        return {row["d"]: int(row["n"]) for row in cur.fetchall()}


def get_ingested_ranges(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    instrument_token: int,
    interval: str,
    from_date: date,
    to_date: date,
) -> list[tuple[date, date]]:
    """Date ranges covered by at least one prior successful ingestion run.

    Used by plan_fetch_ranges to distinguish "never fetched" (needs API call) from
    "fetched but Kite returned no bars" (confirmed market holiday — skip forever).
    """
    sql = """
        SELECT from_date, to_date
        FROM ingestion_runs
        WHERE instrument_token = %s AND interval = %s AND status = 'success'
          AND from_date <= %s AND to_date >= %s
        ORDER BY from_date
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (instrument_token, interval, to_date, from_date))
        return [(row["from_date"], row["to_date"]) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------

def upsert_features(conn: psycopg.Connection, records: list[dict]) -> int:  # type: ignore[type-arg]
    if not records:
        return 0
    sql = """
        INSERT INTO features (ts, instrument_token, interval, feature_set_version, feature_name, value)
        VALUES (%(ts)s, %(instrument_token)s, %(interval)s,
                %(feature_set_version)s, %(feature_name)s, %(value)s)
        ON CONFLICT (instrument_token, interval, feature_set_version, feature_name, ts)
        DO UPDATE SET value = EXCLUDED.value
    """
    with conn.cursor() as cur:
        cur.executemany(sql, records)
    conn.commit()
    return len(records)


def read_features(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    instrument_token: int,
    interval: str,
    feature_set_version: str,
    from_date: date,
    to_date: date,
) -> pd.DataFrame:
    from_dt, to_dt = _ist_day_bounds(from_date, to_date)
    sql = """
        SELECT ts, feature_name, value
        FROM features
        WHERE instrument_token = %s AND interval = %s
          AND feature_set_version = %s AND ts >= %s AND ts < %s
        ORDER BY ts, feature_name
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (instrument_token, interval, feature_set_version, from_dt, to_dt))
        rows = cur.fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    return df.pivot(index="ts", columns="feature_name", values="value").reset_index()


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def log_ingestion_run(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    instrument_token: int,
    tradingsymbol: str,
    interval: str,
    from_date: date,
    to_date: date,
    rows_fetched: int,
    rows_stored: int,
    status: str,
    error_message: Optional[str] = None,
) -> None:
    sql = """
        INSERT INTO ingestion_runs (
            instrument_token, tradingsymbol, interval, from_date, to_date,
            rows_fetched, rows_stored, status, error_message, completed_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            instrument_token, tradingsymbol, interval,
            from_date, to_date,
            rows_fetched, rows_stored, status, error_message,
        ))
    conn.commit()
