"""Reporter SQL reads over the ledger + report persistence (news_store style).

All period filters convert TIMESTAMPTZ columns to IST calendar dates — the same
session convention the trader's snapshot boundary uses, so a report's "day" is an
NSE session, never a UTC split of one.
"""
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import psycopg
import redis

from trading_system.agents.reporter.core import Period
from trading_system.config import get_settings
from trading_system.observability import get_logger

logger = get_logger(__name__)

_IST_SQL = "AT TIME ZONE 'Asia/Kolkata'"
_IST = ZoneInfo("Asia/Kolkata")


def snapshot_dates(conn: psycopg.Connection) -> list[date]:  # type: ignore[type-arg]
    with conn.cursor() as cur:
        cur.execute(f"SELECT DISTINCT (ts {_IST_SQL})::date FROM live_equity ORDER BY 1")
        return [r[0] for r in cur.fetchall()]


def reported_periods(conn: psycopg.Connection) -> set:  # type: ignore[type-arg]
    with conn.cursor() as cur:
        cur.execute("SELECT period_type, period_start FROM reports")
        return {(r[0], r[1]) for r in cur.fetchall()}


def equity_rows(conn: psycopg.Connection, from_d: date, to_d: date) -> list:  # type: ignore[type-arg]
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT (ts {_IST_SQL})::date, sleeve, equity FROM live_equity
                WHERE (ts {_IST_SQL})::date BETWEEN %s AND %s ORDER BY ts""",
            (from_d, to_d),
        )
        return [(r[0], r[1], float(r[2])) for r in cur.fetchall()]


def closed_positions(conn: psycopg.Connection, from_d: date, to_d: date) -> list:  # type: ignore[type-arg]
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT sleeve, symbol, avg_price, realized_pnl, opened_ts, closed_ts,
                       exit_reason
                FROM live_positions
                WHERE closed_ts IS NOT NULL
                  AND (closed_ts {_IST_SQL})::date BETWEEN %s AND %s
                ORDER BY closed_ts""",
            (from_d, to_d),
        )
        return [
            {"sleeve": r[0], "symbol": r[1], "avg_price": float(r[2]),
             "realized_pnl": float(r[3] or 0.0), "opened_ts": str(r[4]),
             "closed_ts": str(r[5]), "exit_reason": r[6]}
            for r in cur.fetchall()
        ]


def total_fees(conn: psycopg.Connection, from_d: date, to_d: date) -> float:  # type: ignore[type-arg]
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT COALESCE(SUM((fees->>'total')::float), 0) FROM live_fills
                WHERE (ts {_IST_SQL})::date BETWEEN %s AND %s""",
            (from_d, to_d),
        )
        return float(cur.fetchone()[0])  # type: ignore[index]


def open_positions_now(conn: psycopg.Connection) -> list:  # type: ignore[type-arg]
    with conn.cursor() as cur:
        cur.execute(
            """SELECT sleeve, symbol, qty, avg_price, opened_ts FROM live_positions
               WHERE closed_ts IS NULL ORDER BY sleeve, symbol"""
        )
        return [
            {"sleeve": r[0], "symbol": r[1], "qty": int(r[2]),
             "avg_price": float(r[3]), "opened_ts": str(r[4].date())}
            for r in cur.fetchall()
        ]


def _count(conn: psycopg.Connection, table: str, ts_col: str,  # type: ignore[type-arg]
           from_d: date, to_d: date) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT COUNT(*) FROM {table}
                WHERE ({ts_col} {_IST_SQL})::date BETWEEN %s AND %s""",
            (from_d, to_d),
        )
        return int(cur.fetchone()[0])  # type: ignore[index]


def signal_count(conn, from_d: date, to_d: date) -> int:  # type: ignore[no-untyped-def]
    return _count(conn, "live_signals", "ts", from_d, to_d)


def order_count(conn, from_d: date, to_d: date) -> int:  # type: ignore[no-untyped-def]
    return _count(conn, "live_orders", "created_at", from_d, to_d)


def fill_count(conn, from_d: date, to_d: date) -> int:  # type: ignore[no-untyped-def]
    return _count(conn, "live_fills", "ts", from_d, to_d)


def news_count(conn, from_d: date, to_d: date) -> int:  # type: ignore[no-untyped-def]
    return _count(conn, "news_items", "fetched_ts", from_d, to_d)


def admissions_between(conn: psycopg.Connection, from_d: date, to_d: date) -> list:  # type: ignore[type-arg]
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT symbol FROM universe_members
                WHERE (admitted_ts {_IST_SQL})::date BETWEEN %s AND %s ORDER BY symbol""",
            (from_d, to_d),
        )
        return [r[0] for r in cur.fetchall()]


def budget_snapshot() -> dict:  # type: ignore[type-arg]
    """Best-effort Redis budget readout — 'n/a' fields are normal (TTL'd keys)."""
    s = get_settings()
    out: dict = {"llm_month_usd": None, "stockinsights_today": None}
    try:
        r = redis.Redis.from_url(s.redis_url, decode_responses=True)
        month = datetime.now(_IST).strftime("%Y-%m")
        spend = r.get(f"spend:llm:{month}")
        if spend is not None:
            out["llm_month_usd"] = int(spend) / 1_000_000
        si = r.get(f"budget:stockinsights:{datetime.now(_IST).date().isoformat()}")
        if si is not None:
            out["stockinsights_today"] = int(si)
    except Exception as exc:
        logger.warning("budget_snapshot_failed", error=str(exc))
    return out


def insert_report(conn: psycopg.Connection, period: Period, payload: dict,  # type: ignore[type-arg]
                  markdown: str, force: bool = False) -> bool:
    """Persist one report row. Returns True if written (idempotent unless force)."""
    conflict = ("DO UPDATE SET period_end=EXCLUDED.period_end, payload=EXCLUDED.payload, "
                "markdown=EXCLUDED.markdown, created_at=NOW()") if force else "DO NOTHING"
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO reports (period_type, period_start, period_end, payload, markdown)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (period_type, period_start) {conflict}""",
            (period.period_type, period.start, period.end,
             json.dumps(payload, default=str), markdown),
        )
        written = cur.rowcount > 0
    conn.commit()
    return written
