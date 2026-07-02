"""Shared test fixtures — no live API or database required."""
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import pytest

from trading_system.replay.events import BarEvent

# Fixed IST offset for generating realistic timestamps (no zoneinfo dependency in tests)
_IST = timezone(timedelta(hours=5, minutes=30))
_UTC = timezone.utc


def make_bar(
    day: int,
    symbol: str = "TEST",
    close: float = 100.0,
    *,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: int = 100_000,
    features: dict[str, float] | None = None,
    month: int = 1,
) -> BarEvent:
    """Build a single daily BarEvent for backtest/strategy tests."""
    o = open_ if open_ is not None else close
    return BarEvent(
        ts=datetime(2024, month, day, tzinfo=_UTC),
        symbol=symbol,
        interval="day",
        instrument_token=abs(hash(symbol)) % 1_000_000,
        open=o,
        high=high if high is not None else max(o, close),
        low=low if low is not None else min(o, close),
        close=close,
        volume=volume,
        features=features or {},
    )


def make_ohlcv_df(
    n: int = 100,
    freq: str = "1min",
    instrument_token: int = 12345,
    interval: str = "minute",
    seed: int = 42,
    start: str = "2024-01-02 09:15",
) -> pd.DataFrame:
    """Synthetic OHLCV DataFrame in UTC (as stored post-cleaning)."""
    rng = np.random.default_rng(seed)
    ts = pd.date_range(start=start, periods=n, freq=freq, tz="Asia/Kolkata").tz_convert("UTC")
    close = 1000.0 + np.cumsum(rng.standard_normal(n) * 2)
    open_ = close + rng.standard_normal(n) * 0.5
    high = np.maximum(open_, close) + np.abs(rng.standard_normal(n)) * 0.3
    low = np.minimum(open_, close) - np.abs(rng.standard_normal(n)) * 0.3
    volume = rng.integers(100_000, 1_000_000, n).astype(int)
    return pd.DataFrame({
        "ts": ts,
        "instrument_token": instrument_token,
        "interval": interval,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })


def make_raw_kite(
    n: int = 10,
    seed: int = 0,
    start: str = "2024-01-02 09:15",
) -> list[dict]:  # type: ignore[type-arg]
    """Raw-format records as returned by kite.historical_data() (naive IST datetimes)."""
    rng = np.random.default_rng(seed)
    ts = pd.date_range(start=start, periods=n, freq="1min")  # naive, IST
    close = 1000.0 + np.cumsum(rng.standard_normal(n) * 2)
    records = []
    for i in range(n):
        o = float(close[i] + rng.standard_normal() * 0.3)
        c = float(close[i])
        h = float(max(o, c) + abs(float(rng.standard_normal())) * 0.2)
        lo = float(min(o, c) - abs(float(rng.standard_normal())) * 0.2)
        records.append({
            "date": ts[i].to_pydatetime(),
            "open": o,
            "high": h,
            "low": lo,
            "close": c,
            "volume": int(rng.integers(100_000, 500_000)),
        })
    return records


@pytest.fixture
def sample_ohlcv() -> pd.DataFrame:
    return make_ohlcv_df()


@pytest.fixture
def sample_daily_ohlcv() -> pd.DataFrame:
    return make_ohlcv_df(n=252, freq="B", interval="day", start="2024-01-02 15:30")
