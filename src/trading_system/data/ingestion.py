"""Historical ingestion orchestration.

Flow per (symbol, interval, date-range):
  plan missing ranges → fetch_historical → archive_raw → clean_ohlcv → upsert_ohlcv → log run

Idempotent: re-running the same range inserts nothing (ON CONFLICT DO NOTHING).

Gap-aware: before calling the Kite API, the existing DB coverage is diffed against the NSE
trading calendar. Only days that are missing (or incompletely stored) are fetched, coalesced
into contiguous ranges. A fully-covered range makes zero API calls. The current day is always
re-fetched (its session may still be in progress — the Reader tops it up hourly). Pass
force=True to bypass the check and re-pull the whole range.

Parallel execution: multiple symbols are ingested concurrently via ThreadPoolExecutor.
All workers share the same token-bucket rate limiter in kite_client, so the combined
request rate never exceeds kite_historical_rps regardless of worker count.
Each worker opens its own DB connection (psycopg connections are not thread-safe).
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from zoneinfo import ZoneInfo

import psycopg
from kiteconnect import KiteConnect

from trading_system.data.auth import get_authenticated_kite
from trading_system.data.calendar import bars_per_session, get_trading_days
from trading_system.data.cleaning import clean_ohlcv
from trading_system.data.kite_client import fetch_historical
from trading_system.data.raw_archive import archive_raw
from trading_system.data.storage import (
    get_connection,
    get_ingested_ranges,
    get_ohlcv_bar_counts,
    get_symbol_token_map,
    log_ingestion_run,
    upsert_ohlcv,
)
from trading_system.features.store import build_features_after_ingest
from trading_system.observability import get_logger

logger = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# A stored day counts as "covered" if it has at least this fraction of a full session's bars.
# Below 1.0 so minor real gaps (brief halts, missing ticks) don't trigger perpetual re-fetching.
_COVERAGE_THRESHOLD = 0.9


def plan_fetch_ranges(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    instrument_token: int,
    interval: str,
    from_date: date,
    to_date: date,
    today: date | None = None,
) -> list[tuple[date, date]]:
    """Diff DB coverage against the NSE calendar → contiguous (from, to) ranges to fetch.

    Three-way classification for each NSE trading day:
      1. Bar count >= threshold → fully covered; only re-fetch if it is today (live session).
      2. Bar count == 0 AND a prior successful ingestion run covers this date → confirmed
         market holiday (Kite has no data for it) — treat as covered; only re-fetch if today.
      3. Everything else (bar count 0 and never fetched, or partial bars) → needs fetching.

    Consecutive needy days are coalesced into one contiguous range. Returns [] when fully
    covered, meaning zero Kite API calls will be made.
    """
    today = today or datetime.now(IST).date()
    trading_days = get_trading_days(from_date, to_date)
    if not trading_days:
        return []

    counts = get_ohlcv_bar_counts(conn, instrument_token, interval, from_date, to_date)
    threshold = max(1, int(bars_per_session(interval) * _COVERAGE_THRESHOLD))
    prior_runs = get_ingested_ranges(conn, instrument_token, interval, from_date, to_date)

    def _previously_fetched(d: date) -> bool:
        return any(r_from <= d <= r_to for r_from, r_to in prior_runs)

    ranges: list[tuple[date, date]] = []
    run_start: date | None = None
    run_end: date | None = None
    for d in trading_days:
        bar_count = counts.get(d, 0)
        if bar_count >= threshold or (bar_count == 0 and _previously_fetched(d)):
            # Covered (either has bars, or confirmed holiday from a prior run).
            needs_fetch = d >= today
        else:
            needs_fetch = True

        if needs_fetch:
            if run_start is None:
                run_start = d
            run_end = d
        elif run_start is not None:
            ranges.append((run_start, run_end))  # type: ignore[arg-type]
            run_start = run_end = None
    if run_start is not None:
        ranges.append((run_start, run_end))  # type: ignore[arg-type]
    return ranges


def ingest_symbol(
    kite: KiteConnect,
    conn: psycopg.Connection,  # type: ignore[type-arg]
    symbol: str,
    instrument_token: int,
    interval: str,
    from_date: date,
    to_date: date,
    *,
    auto_features: bool = True,
    force: bool = False,
) -> dict:  # type: ignore[type-arg]
    logger.info("ingest_start", symbol=symbol, interval=interval, from_=str(from_date), to=str(to_date))  # noqa: E501
    rows_fetched = 0
    rows_stored = 0
    status = "failed"
    error_message: str | None = None

    # Gap-fill: only hit the API for ranges not already covered in the DB (force bypasses).
    if force:
        fetch_ranges: list[tuple[date, date]] = [(from_date, to_date)]
    else:
        fetch_ranges = plan_fetch_ranges(conn, instrument_token, interval, from_date, to_date)

    if not fetch_ranges:
        logger.info("ingest_up_to_date", symbol=symbol, interval=interval,
                    from_=str(from_date), to=str(to_date))
        # No API call, no new rows — features are assumed already built for covered data.
        return {
            "symbol": symbol,
            "interval": interval,
            "rows_fetched": 0,
            "rows_stored": 0,
            "status": "up_to_date",
            "features_stored": {},
            "ranges_fetched": 0,
        }

    try:
        for r_from, r_to in fetch_ranges:
            raw = fetch_historical(kite, instrument_token, r_from, r_to, interval)
            rows_fetched += len(raw)
            archive_raw(symbol, interval, r_from, r_to, raw)
            result = clean_ohlcv(raw, instrument_token, interval, symbol)
            rows_stored += upsert_ohlcv(conn, result.df)
        status = "success"

        logger.info(
            "ingest_done",
            symbol=symbol,
            interval=interval,
            ranges=len(fetch_ranges),
            fetched=rows_fetched,
            stored=rows_stored,
        )
    except Exception as exc:
        error_message = str(exc)
        logger.error("ingest_failed", symbol=symbol, interval=interval, error=error_message)
        raise
    finally:
        log_ingestion_run(
            conn, instrument_token, symbol, interval,
            from_date, to_date, rows_fetched, rows_stored, status, error_message,
        )

    features_stored: dict[str, int] = {}
    if auto_features and status == "success":
        features_stored = build_features_after_ingest(
            conn, instrument_token, symbol, interval, from_date, to_date
        )

    return {
        "symbol": symbol,
        "interval": interval,
        "rows_fetched": rows_fetched,
        "rows_stored": rows_stored,
        "status": status,
        "features_stored": features_stored,
        "ranges_fetched": len(fetch_ranges),
    }


def _ingest_one(
    kite: KiteConnect,
    symbol: str,
    token: int,
    interval: str,
    from_date: date,
    to_date: date,
    auto_features: bool = True,
    force: bool = False,
) -> dict:  # type: ignore[type-arg]
    """Worker function: opens its own DB connection so threads don't share one."""
    with get_connection() as conn:
        return ingest_symbol(
            kite, conn, symbol, token, interval, from_date, to_date,
            auto_features=auto_features, force=force,
        )


def ingest_universe(
    symbols: list[str],
    intervals: list[str],
    from_date: date,
    to_date: date,
    workers: int = 5,
    auto_features: bool = True,
    force: bool = False,
) -> list[dict]:  # type: ignore[type-arg]
    """Ingest all (symbol × interval) combinations in parallel. Skips unknown symbols.

    Gap-aware (force=False, default): days already covered in the DB are not re-fetched, so
    re-running an overlapping range makes no redundant Kite API calls. force=True re-pulls the
    whole range regardless of coverage.

    When auto_features=True (default), computes and upserts all matching feature sets
    immediately after each symbol's OHLCV lands in the DB — no separate build-features
    step required.

    The Zerodha rate limit (default 3 RPS) is shared across all workers via the
    module-level token-bucket in kite_client — more workers do not increase API load.
    The speedup comes from overlapping data-processing and DB I/O across symbols.
    """
    kite = get_authenticated_kite()

    with get_connection() as conn:
        token_map = get_symbol_token_map(conn, symbols)

    missing = set(symbols) - set(token_map)
    if missing:
        logger.warning(
            "symbols_not_in_db",
            missing=sorted(missing),
            hint="Run `trading sync-instruments` first.",
        )

    tasks = [
        (symbol, token_map[symbol], interval)
        for symbol in symbols
        for interval in intervals
        if token_map.get(symbol)
    ]
    results: list[dict] = [  # type: ignore[type-arg]
        {"symbol": s, "status": "skipped_no_token"}
        for s in symbols
        if not token_map.get(s)
    ]

    if workers <= 1 or len(tasks) <= 1:
        with get_connection() as conn:
            for symbol, token, interval in tasks:
                try:
                    results.append(
                        ingest_symbol(
                            kite, conn, symbol, token, interval, from_date, to_date,
                            auto_features=auto_features, force=force,
                        )
                    )
                except Exception:
                    results.append({"symbol": symbol, "interval": interval, "status": "failed"})
        return results

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(
                _ingest_one, kite, sym, tok, iv, from_date, to_date, auto_features, force
            ): (sym, iv)
            for sym, tok, iv in tasks
        }
        for future in as_completed(future_map):
            sym, iv = future_map[future]
            try:
                results.append(future.result())
            except Exception:
                results.append({"symbol": sym, "interval": iv, "status": "failed"})

    return results
