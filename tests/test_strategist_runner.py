"""StrategistRunner: interval routing, cross-sectional batching, deterministic ids."""
from datetime import datetime, timezone

from trading_system.agents.strategist.runner import Sleeve, StrategistRunner
from trading_system.replay.events import BarEvent
from trading_system.strategy.base import Strategy
from trading_system.strategy.signals import Side, Signal

_UTC = timezone.utc


class FakeView:
    def has_position(self, s): return False
    def position_qty(self, s): return 0
    def entry_price(self, s): return 0.0
    def bars_held(self, s): return 0
    def open_symbols(self): return []


class PerBarBuyer(Strategy):
    name = "per_bar"
    version = "t"
    interval = "5minute"

    def on_bar(self, bar, portfolio):
        return [Signal(symbol=bar.symbol, side=Side.BUY, reason="pb", size_hint=0.1)]


class BatchCounter(Strategy):
    name = "batch"
    version = "t"
    interval = "day"
    cross_sectional = True

    def __init__(self):
        super().__init__()
        self.batches: list[list] = []

    def on_bar(self, bar, portfolio):
        return []

    def on_bars(self, bars, portfolio):
        self.batches.append([b.symbol for b in bars])
        return [Signal(symbol=bars[0].symbol, side=Side.BUY, reason="xb", size_hint=0.05)]


def _bar(day, symbol, interval="day", close=100.0):
    return BarEvent(ts=datetime(2026, 7, day, tzinfo=_UTC), symbol=symbol, interval=interval,
                    instrument_token=1, open=close, high=close, low=close, close=close,
                    volume=1000, features={})


def test_routes_by_interval():
    out = []
    runner = StrategistRunner(
        [Sleeve("intra", PerBarBuyer(), "5minute", FakeView())], out.append)
    assert runner.on_bar(_bar(1, "A", interval="day")) == 0     # wrong interval → ignored
    assert runner.on_bar(_bar(1, "A", interval="5minute")) == 1
    assert out[0].sleeve == "intra" and out[0].side == "BUY"


def test_cross_sectional_batches_flush_on_ts_advance():
    out = []
    strat = BatchCounter()
    runner = StrategistRunner([Sleeve("mom", strat, "day", FakeView())], out.append)
    runner.on_bar(_bar(1, "A"))
    runner.on_bar(_bar(1, "B"))          # same ts — buffered, nothing published yet
    assert out == []
    runner.on_bar(_bar(2, "A"))          # ts advances → day-1 batch flushes
    assert strat.batches == [["A", "B"]]
    assert len(out) == 1
    runner.flush()                        # drain the day-2 batch
    assert strat.batches[-1] == ["A"]


def test_signal_ids_deterministic_and_unique_per_side():
    out = []
    runner = StrategistRunner(
        [Sleeve("intra", PerBarBuyer(), "5minute", FakeView())], out.append)
    runner.on_bar(_bar(1, "A", interval="5minute"))
    runner.on_bar(_bar(1, "A", interval="5minute"))  # same bar re-fed (redelivery)
    assert out[0].id == out[1].id                     # dedupe key survives redelivery
    runner.on_bar(_bar(2, "A", interval="5minute"))
    assert out[2].id != out[0].id
