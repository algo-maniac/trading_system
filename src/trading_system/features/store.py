"""Feature store: compute indicators from stored OHLCV and persist to DB."""
import math
from datetime import date, timedelta

import pandas as pd
import psycopg

from trading_system.data.storage import read_ohlcv, upsert_features
from trading_system.features.engineering import compute_features, compute_intraday_features
from trading_system.features.registry import DEFAULT_FEATURE_SET, FEATURE_SETS
from trading_system.observability import get_logger

logger = get_logger(__name__)

# NSE session = 09:15–15:30 = 375 trading minutes per day
_TRADING_MINS_PER_DAY = 375.0

# Minutes per candle bar for known Kite intervals
_BAR_MINS: dict[str, float] = {
    "minute": 1.0,
    "3minute": 3.0,
    "5minute": 5.0,
    "10minute": 10.0,
    "15minute": 15.0,
    "30minute": 30.0,
    "60minute": 60.0,
}


def _warmup_lookback_days(interval: str, warmup_bars: int) -> int:
    """Calendar days to prepend before from_date so warmup_bars of OHLCV are available.

    Uses a 1.6× buffer over the raw trading-day estimate to absorb weekends and holidays.
    Daily intervals use a 2× buffer (weekends + NSE holidays ≈ 70 non-trading days/yr).
    """
    if interval == "day":
        return warmup_bars * 2
    mins = _BAR_MINS.get(interval, 5.0)
    trading_days = warmup_bars * mins / _TRADING_MINS_PER_DAY
    return max(3, math.ceil(trading_days * 1.6))


def build_features_for_symbol(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    instrument_token: int,
    symbol: str,
    interval: str,
    from_date: date,
    to_date: date,
    feature_set_version: str = DEFAULT_FEATURE_SET,
) -> int:
    """Compute versioned features for one symbol/interval and upsert into DB.

    Returns the number of feature records stored (not counting NaN warmup bars).
    """
    config = FEATURE_SETS[feature_set_version]
    logger.info("build_features_start", symbol=symbol, interval=interval, version=feature_set_version)

    ohlcv = read_ohlcv(conn, instrument_token, interval, from_date, to_date)
    if ohlcv.empty:
        logger.warning("build_features_no_ohlcv", symbol=symbol, interval=interval)
        return 0

    ohlcv = ohlcv.sort_values("ts").reset_index(drop=True)
    # Intraday sets need session-aware features (VWAP/OR reset per session); daily sets use the
    # continuous path. Selected by the feature-set config so the daily path is byte-identical.
    feat_df = compute_intraday_features(ohlcv) if config.intraday else compute_features(ohlcv)

    # Melt wide → long; skip NaN rows (warmup) entirely rather than storing NULL
    records = []
    for _, row in feat_df.iterrows():
        ts = row["ts"]
        for feat_name in config.feature_names:
            val = row.get(feat_name)
            if val is not None and pd.notna(val):
                records.append({
                    "ts": ts,
                    "instrument_token": instrument_token,
                    "interval": interval,
                    "feature_set_version": feature_set_version,
                    "feature_name": feat_name,
                    "value": float(val),
                })

    count = upsert_features(conn, records)
    logger.info("build_features_done", symbol=symbol, interval=interval, version=feature_set_version, records=count)
    return count


def build_features_after_ingest(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    instrument_token: int,
    symbol: str,
    interval: str,
    from_date: date,
    to_date: date,
) -> dict[str, int]:
    """Build all feature sets appropriate for *interval* immediately after OHLCV ingestion.

    Automatically selects intraday feature sets for sub-day intervals and daily feature sets
    for the 'day' interval.  Each set's from_date is extended backward by its warmup window
    so the first computed bar has a valid (non-NaN) indicator value.

    Returns {feature_set_version: records_stored}.  Failures per version are logged and
    skipped rather than aborting — the OHLCV store is always the primary deliverable.
    """
    is_intraday = interval != "day"
    results: dict[str, int] = {}
    for version, config in FEATURE_SETS.items():
        if config.intraday != is_intraday:
            continue
        lookback = _warmup_lookback_days(interval, config.warmup_bars)
        feature_from = from_date - timedelta(days=lookback)
        try:
            count = build_features_for_symbol(
                conn, instrument_token, symbol, interval, feature_from, to_date, version
            )
            results[version] = count
        except Exception as exc:
            logger.error(
                "build_features_after_ingest_failed",
                symbol=symbol, interval=interval, version=version, error=str(exc),
            )
    return results
