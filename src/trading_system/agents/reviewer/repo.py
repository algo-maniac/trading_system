"""Postgres side of the reviewer: point-in-time tool data + verdict persistence.

PostgresReviewTools is bound to one signal's timestamp — every query filters on
`<= as_of`, so a review run during replay sees exactly what it would have seen live.
"""
import math
from datetime import datetime, timedelta
from typing import Any

import psycopg

from trading_system.bus.signals import SignalEvent
from trading_system.llm.signal_review import SignalVerdict

_NEWS_LIMIT = 10


class PostgresReviewTools:
    def __init__(self, conn: psycopg.Connection, as_of: datetime) -> None:  # type: ignore[type-arg]
        self._conn = conn
        self._as_of = as_of

    def position(self, symbol: str) -> dict[str, Any]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT sleeve, qty, avg_price, opened_ts FROM live_positions "
                "WHERE symbol=%s AND closed_ts IS NULL ORDER BY id", (symbol,))
            rows = cur.fetchall()
        return {"symbol": symbol, "positions": [
            {"sleeve": r[0], "qty": int(r[1]), "avg_price": float(r[2]),
             "opened_ts": r[3].isoformat()} for r in rows]}

    def recent_news(self, symbol: str, days: int) -> dict[str, Any]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT published_ts, announcement_type, sentiment, summary_header "
                "FROM news_items WHERE nse_symbol=%s AND published_ts <= %s "
                "AND published_ts > %s ORDER BY published_ts DESC LIMIT %s",
                (symbol, self._as_of, self._as_of - timedelta(days=days), _NEWS_LIMIT))
            rows = cur.fetchall()
        return {"symbol": symbol, "days": days, "items": [
            {"published": r[0].isoformat(), "type": r[1], "sentiment": r[2], "header": r[3]}
            for r in rows]}

    def price_history(self, symbol: str, bars: int) -> dict[str, Any]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT o.ts, o.close, o.volume FROM ohlcv o "
                "JOIN instruments i ON i.instrument_token = o.instrument_token "
                "WHERE i.tradingsymbol=%s AND i.exchange='NSE' AND o.interval='day' "
                "AND o.ts <= %s ORDER BY o.ts DESC LIMIT %s",
                (symbol, self._as_of, bars))
            rows = list(reversed(cur.fetchall()))
        return summarize_closes(symbol, [(r[0].date().isoformat(), float(r[1]), int(r[2]))
                                         for r in rows])

    def sleeve_exposure(self, sleeve: str) -> dict[str, Any]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT cash FROM live_sleeves WHERE sleeve=%s", (sleeve,))
            row = cur.fetchone()
            cur.execute("SELECT symbol FROM live_positions WHERE sleeve=%s "
                        "AND closed_ts IS NULL ORDER BY id", (sleeve,))
            held = [r[0] for r in cur.fetchall()]
        return {"sleeve": sleeve, "cash": float(row[0]) if row else None,
                "open_positions": len(held), "symbols": held}


def summarize_closes(symbol: str, bars: list[tuple[str, float, int]]) -> dict[str, Any]:
    """Pure: daily (date, close, volume) oldest→newest → the price_history tool payload."""
    closes = [c for _, c, _ in bars]
    out: dict[str, Any] = {"symbol": symbol, "bars": [
        {"date": d, "close": c, "volume": v} for d, c, v in bars],
        "return_20d": None, "volatility_20d": None, "pct_below_high": None}
    if len(closes) >= 21:
        out["return_20d"] = round(closes[-1] / closes[-21] - 1.0, 4)
        rets = [closes[i] / closes[i - 1] - 1.0 for i in range(len(closes) - 20, len(closes))]
        mean = sum(rets) / len(rets)
        out["volatility_20d"] = round(math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets)), 4)
    if closes:
        out["pct_below_high"] = round(1.0 - closes[-1] / max(closes), 4)
    return out


def has_review(conn: psycopg.Connection, signal_id: str) -> bool:  # type: ignore[type-arg]
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM signal_reviews WHERE signal_id=%s", (signal_id,))
        return cur.fetchone() is not None


def save_review(conn: psycopg.Connection, sig: SignalEvent,  # type: ignore[type-arg]
                verdict: SignalVerdict, reviewer: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO signal_reviews (signal_id, ts, sleeve, strategy, symbol, side, "
            "ref_price, approve, confidence, reasoning, tools_used, cost_usd, reviewer, error) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (signal_id) DO NOTHING",
            (sig.id, sig.ts, sig.sleeve, sig.strategy, sig.symbol, sig.side, sig.ref_price,
             verdict.approve, verdict.confidence, verdict.reasoning, verdict.tools_used,
             verdict.cost_usd, reviewer, verdict.error))
        return cur.rowcount == 1
