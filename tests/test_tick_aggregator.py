"""Tick→1min aggregation + 1min→5min resampling: OHLCV correctness, volume deltas,
minute/bucket rollovers, flush."""
from datetime import datetime

from trading_system.agents.reader.aggregator import (
    FiveMinResampler, TickAggregator, five_min_bucket,
)
from trading_system.replay.events import BarEvent

T = 12345
SYM = {T: "RELIANCE"}


def _ts(hh, mm, ss=0):
    return datetime(2026, 7, 3, hh, mm, ss)


def test_ticks_roll_into_ohlcv_minute_bar():
    agg = TickAggregator(SYM)
    assert agg.on_tick(T, 100.0, 1000, _ts(9, 15, 1)) is None
    assert agg.on_tick(T, 102.0, 1500, _ts(9, 15, 20)) is None
    assert agg.on_tick(T, 99.0, 1800, _ts(9, 15, 45)) is None
    bar = agg.on_tick(T, 101.0, 2100, _ts(9, 16, 2))   # minute rolls → 9:15 bar completes
    assert bar is not None
    # close is the LAST tick inside 9:15 (99.0) — the 101.0 tick opened the 9:16 bar
    assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 102.0, 99.0, 99.0)
    assert bar.volume == 1800          # first bar takes full cumulative (session start)
    assert bar.ts == _ts(9, 15) and bar.interval == "minute"


def test_volume_is_cumulative_delta_after_first_bar():
    agg = TickAggregator(SYM)
    agg.on_tick(T, 100.0, 1000, _ts(9, 15, 1))
    agg.on_tick(T, 100.5, 4000, _ts(9, 16, 1))    # closes bar1 (vol 1000)
    bar2 = agg.on_tick(T, 101.0, 9000, _ts(9, 17, 1))
    assert bar2.volume == 4000 - 1000


def test_flush_closes_open_bars():
    agg = TickAggregator(SYM)
    agg.on_tick(T, 100.0, 500, _ts(15, 29, 30))
    bars = agg.flush()
    assert len(bars) == 1 and bars[0].close == 100.0
    assert agg.flush() == []


def test_unknown_token_and_zero_price_ignored():
    agg = TickAggregator(SYM)
    assert agg.on_tick(999, 100.0, 10, _ts(9, 15)) is None
    assert agg.on_tick(T, 0.0, 10, _ts(9, 15)) is None
    assert agg.flush() == []


def test_five_min_bucket_nse_grid():
    assert five_min_bucket(_ts(9, 15)) == _ts(9, 15)
    assert five_min_bucket(_ts(9, 19)) == _ts(9, 15)
    assert five_min_bucket(_ts(9, 20)) == _ts(9, 20)
    assert five_min_bucket(_ts(15, 29)) == _ts(15, 25)


def _minbar(hh, mm, o, h, lo, c, v):
    return BarEvent(ts=_ts(hh, mm), symbol="RELIANCE", interval="minute", instrument_token=T,
                    open=o, high=h, low=lo, close=c, volume=v, features={})


def test_resampler_folds_five_minutes():
    rs = FiveMinResampler()
    for mm, px in zip(range(15, 20), [100, 101, 99, 102, 101.5], strict=True):
        assert rs.on_minute_bar(_minbar(9, mm, px, px + 1, px - 1, px, 100)) is None
    bar5 = rs.on_minute_bar(_minbar(9, 20, 103, 104, 102, 103, 100))  # next bucket
    assert bar5 is not None
    assert bar5.ts == _ts(9, 15) and bar5.interval == "5minute"
    assert bar5.open == 100 and bar5.close == 101.5
    assert bar5.high == 103 and bar5.low == 98
    assert bar5.volume == 500
