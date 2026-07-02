"""Unit tests for replay engine primitives — no live DB required.

Full end-to-end replay determinism (hash comparison over stored data) is
verified via `trading verify` against a live TimescaleDB instance.
"""
import hashlib
import json
from datetime import datetime, timezone

import pytest

from trading_system.replay.events import BarEvent


def _make_bar(ts: datetime, symbol: str, close: float = 100.0) -> BarEvent:
    return BarEvent(
        ts=ts,
        symbol=symbol,
        interval="day",
        instrument_token=abs(hash(symbol)) % 1_000_000,
        open=close - 1.0,
        high=close + 1.0,
        low=close - 2.0,
        close=close,
        volume=500_000,
    )


_UTC = timezone.utc


def test_bar_ordering_by_ts():
    ts1 = datetime(2024, 1, 1, tzinfo=_UTC)
    ts2 = datetime(2024, 1, 2, tzinfo=_UTC)
    bar1 = _make_bar(ts1, "AAA")
    bar2 = _make_bar(ts2, "AAA")
    assert bar1 < bar2


def test_bar_ordering_by_symbol_same_ts():
    ts = datetime(2024, 1, 1, tzinfo=_UTC)
    bar_a = _make_bar(ts, "AAA")
    bar_b = _make_bar(ts, "BBB")
    assert bar_a < bar_b  # "AAA" < "BBB" lexicographically


def test_bar_frozen():
    bar = _make_bar(datetime(2024, 1, 1, tzinfo=_UTC), "TEST")
    with pytest.raises((AttributeError, TypeError)):
        bar.close = 999.0  # type: ignore[misc]


def test_bar_with_features():
    bar = BarEvent(
        ts=datetime(2024, 1, 1, tzinfo=_UTC),
        symbol="RELIANCE",
        interval="day",
        instrument_token=123,
        open=2800.0,
        high=2850.0,
        low=2780.0,
        close=2830.0,
        volume=1_000_000,
        features={"rsi_14": 62.5, "sma_20": 2790.0},
    )
    assert bar.features["rsi_14"] == 62.5


def test_features_not_included_in_ordering():
    ts = datetime(2024, 1, 1, tzinfo=_UTC)
    bar1 = _make_bar(ts, "X")
    bar2 = BarEvent(
        ts=ts, symbol="X", interval="day", instrument_token=bar1.instrument_token,
        open=bar1.open, high=bar1.high, low=bar1.low, close=bar1.close, volume=bar1.volume,
        features={"rsi_14": 99.9},
    )
    # Same bar with different features should compare equal
    assert bar1 == bar2


def test_hash_stability():
    """Same bar produces the same hash bytes every time."""
    bar = _make_bar(datetime(2024, 1, 2, tzinfo=_UTC), "RELIANCE", close=2500.0)

    def bar_hash(b: BarEvent) -> str:
        h = hashlib.sha256()
        payload = json.dumps({
            "ts":       b.ts.isoformat(),
            "symbol":   b.symbol,
            "interval": b.interval,
            "open":     b.open,
            "high":     b.high,
            "low":      b.low,
            "close":    b.close,
            "volume":   b.volume,
        }, sort_keys=True)
        h.update(payload.encode())
        return h.hexdigest()

    assert bar_hash(bar) == bar_hash(bar)


def test_sorted_bars_are_deterministic():
    ts = datetime(2024, 1, 1, tzinfo=_UTC)
    bars = [
        _make_bar(ts, "TCS"),
        _make_bar(ts, "RELIANCE"),
        _make_bar(ts, "INFY"),
    ]
    run1 = sorted(bars)
    run2 = sorted(bars)
    assert run1 == run2
    assert [b.symbol for b in run1] == ["INFY", "RELIANCE", "TCS"]
