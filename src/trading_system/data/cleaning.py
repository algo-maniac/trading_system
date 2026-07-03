"""OHLCV cleaning: normalization, dedup, sanity checks, gap detection.

Design principles:
- Invalid rows are rejected and logged, never silently fixed.
- Missing bars are flagged and logged, never fabricated.
- Timestamps are always stored as UTC (Kite returns naive IST datetimes).
"""
from typing import NamedTuple

import pandas as pd

from trading_system.data.calendar import get_trading_days
from trading_system.observability import get_logger

logger = get_logger(__name__)


class CleaningResult(NamedTuple):
    df: pd.DataFrame
    rejected_rows: int
    duplicate_count: int
    gap_count: int


def clean_ohlcv(
    raw: list[dict],  # type: ignore[type-arg]
    instrument_token: int,
    interval: str,
    symbol: str = "",
) -> CleaningResult:
    if not raw:
        return CleaningResult(pd.DataFrame(), 0, 0, 0)

    df = pd.DataFrame(raw)
    # Kite returns 'date' column as naive datetime in IST
    df = df.rename(columns={"date": "ts"})

    if not pd.api.types.is_datetime64_any_dtype(df["ts"]):
        df["ts"] = pd.to_datetime(df["ts"])

    # Normalize to UTC.
    # Kite returns naive IST for minute bars, but tz-aware datetimes for daily bars.
    if df["ts"].dt.tz is None:
        df["ts"] = df["ts"].dt.tz_localize("Asia/Kolkata", ambiguous="infer").dt.tz_convert("UTC")
    else:
        df["ts"] = df["ts"].dt.tz_convert("UTC")

    # --- Dedup ---
    df = df.sort_values("ts")
    before = len(df)
    df = df.drop_duplicates(subset=["ts"])
    duplicate_count = before - len(df)
    if duplicate_count:
        logger.warning("duplicates_removed", symbol=symbol, interval=interval, n=duplicate_count)

    # --- OHLC sanity ---
    valid = (
        (df["high"] >= df["open"]) &
        (df["high"] >= df["close"]) &
        (df["high"] >= df["low"]) &
        (df["low"] <= df["open"]) &
        (df["low"] <= df["close"]) &
        (df["volume"] >= 0) &
        df[["open", "high", "low", "close"]].notna().all(axis=1)
    )
    rejected = int((~valid).sum())
    if rejected:
        bad = df[~valid][["ts", "open", "high", "low", "close", "volume"]].head(5)
        logger.warning(
            "ohlc_rejected",
            symbol=symbol,
            interval=interval,
            n=rejected,
            sample=bad.to_dict(orient="records"),
        )
    df = df[valid].copy()

    # --- Metadata columns ---
    df["instrument_token"] = instrument_token
    df["interval"] = interval

    # --- Gap detection (log only, never fabricate) ---
    gap_count = 0
    if interval == "day" and not df.empty:
        ist_dates = set(df["ts"].dt.tz_convert("Asia/Kolkata").dt.date)
        expected = set(get_trading_days(df["ts"].min().date(), df["ts"].max().date()))
        missing = expected - ist_dates
        if missing:
            gap_count = len(missing)
            logger.warning(
                "data_gaps",
                symbol=symbol,
                interval=interval,
                total_missing=gap_count,
                sample=sorted(str(d) for d in missing)[:10],
            )

    return CleaningResult(df=df, rejected_rows=rejected, duplicate_count=duplicate_count, gap_count=gap_count)
