"""Repositories for the Phase 5c tables (news_items, gap_watchlist, focus_list,
universe_members). Same style as execution/ledger.py: idempotent single-statement
writes (ON CONFLICT DO NOTHING) so sweep re-runs and at-least-once semantics are free.
"""
import json
from datetime import date

import psycopg

from trading_system.data.stockinsights_client import NewsItem
from trading_system.observability import get_logger

logger = get_logger(__name__)


def insert_news_items(conn: psycopg.Connection, items: list[NewsItem]) -> int:  # type: ignore[type-arg]
    """Insert deduped by source_link_hash; returns how many were actually new."""
    new = 0
    with conn.cursor() as cur:
        for item in items:
            if not item.source_link:
                continue
            cur.execute(
                """
                INSERT INTO news_items (source_link_hash, source_link, nse_symbol,
                    company_name, announcement_type_id, announcement_type, sentiment,
                    summary_header, summary_text, published_ts, raw)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_link_hash) DO NOTHING
                """,
                (item.source_link_hash, item.source_link, item.nse_symbol,
                 item.company_name, item.announcement_type_id, item.announcement_type,
                 item.sentiment, item.summary_header, item.summary_text,
                 item.published_ts, json.dumps(item.raw, default=str)),
            )
            new += cur.rowcount
    conn.commit()
    return new


def news_id_for_hash(conn: psycopg.Connection, link_hash: str) -> int | None:  # type: ignore[type-arg]
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM news_items WHERE source_link_hash=%s", (link_hash,))
        row = cur.fetchone()
        return row[0] if row else None


def upsert_gap_watch(conn: psycopg.Connection, watch_date: date, symbol: str,  # type: ignore[type-arg]
                     direction: str, news_id: int | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO gap_watchlist (watch_date, symbol, direction, news_id) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (watch_date, symbol) DO NOTHING",
            (watch_date, symbol, direction, news_id),
        )
    conn.commit()


def write_focus_list(conn: psycopg.Connection, focus_date: date,  # type: ignore[type-arg]
                     entries: dict[str, list[str]]) -> int:
    """entries: symbol -> provenance sources. Idempotent per (date, symbol)."""
    with conn.cursor() as cur:
        for symbol, sources in entries.items():
            cur.execute(
                "INSERT INTO focus_list (focus_date, symbol, sources) VALUES (%s, %s, %s) "
                "ON CONFLICT (focus_date, symbol) DO NOTHING",
                (focus_date, symbol, sources),
            )
    conn.commit()
    return len(entries)


def read_focus_list(conn: psycopg.Connection, focus_date: date) -> list[str]:  # type: ignore[type-arg]
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM focus_list WHERE focus_date=%s ORDER BY id",
                    (focus_date,))
        return [r[0] for r in cur.fetchall()]


def admit_universe_member(conn: psycopg.Connection, symbol: str, provenance: dict) -> bool:  # type: ignore[type-arg]
    """Add an ACTIVE member (one active row per symbol enforced by partial unique index).
    Returns False if the symbol is already active (no-op)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO universe_members (symbol, provenance) VALUES (%s, %s) "
            "ON CONFLICT (symbol) WHERE active DO NOTHING",
            (symbol, json.dumps(provenance, default=str)),
        )
        added = cur.rowcount > 0
    conn.commit()
    return added


def active_universe_members(conn: psycopg.Connection) -> list[str]:  # type: ignore[type-arg]
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM universe_members WHERE active ORDER BY symbol")
        return [r[0] for r in cur.fetchall()]
