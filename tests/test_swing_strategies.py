"""Swing strategy logic: pullback-to-trend + N-day breakout entry/exit gating."""
from tests.conftest import make_bar
from trading_system.strategy.signals import Side
from trading_system.strategy.swing_breakout import SwingBreakout
from trading_system.strategy.swing_pullback import SwingPullback


class FakePortfolio:
    def __init__(self, holding=False, bars=0):
        self._holding, self._bars = holding, bars

    def has_position(self, symbol): return self._holding
    def position_qty(self, symbol): return 100 if self._holding else 0
    def entry_price(self, symbol): return 100.0
    def bars_held(self, symbol): return self._bars
    def open_symbols(self): return ["TEST"] if self._holding else []


# --- Swing pullback ---

def test_pullback_enters_uptrend_dip_not_overbought():
    s = SwingPullback(rsi_max=45)
    # close>sma_200 (uptrend), close<=sma_20 (dip), rsi<=45
    f = {"sma_200": 90.0, "sma_50": 98.0, "sma_20": 101.0, "rsi_14": 40.0, "atr_14": 2.0}
    sigs = s.on_bar(make_bar(1, close=100.0, features=f), FakePortfolio())
    assert len(sigs) == 1 and sigs[0].side == Side.BUY
    assert sigs[0].trailing_atr_mult == 3.0


def test_pullback_no_entry_in_downtrend_or_no_dip():
    s = SwingPullback(rsi_max=45)
    pf = FakePortfolio()
    down = {"sma_200": 110.0, "sma_50": 105.0, "sma_20": 102.0, "rsi_14": 40.0, "atr_14": 2.0}
    assert s.on_bar(make_bar(1, close=100.0, features=down), pf) == []           # below sma_200
    no_dip = {"sma_200": 90.0, "sma_50": 95.0, "sma_20": 96.0, "rsi_14": 40.0, "atr_14": 2.0}
    assert s.on_bar(make_bar(1, close=100.0, features=no_dip), pf) == []          # close > sma_20 (no dip)


def test_pullback_exits_on_trend_break_and_max_hold():
    s = SwingPullback(max_hold=10)
    f = {"sma_200": 90.0, "sma_50": 102.0, "sma_20": 101.0, "rsi_14": 50.0, "atr_14": 2.0}
    # close < sma_50 → trend break
    sigs = s.on_bar(make_bar(2, close=100.0, features=f), FakePortfolio(holding=True))
    assert sigs and sigs[0].side == Side.SELL and "trend_break" in sigs[0].reason
    # held >= max_hold → time exit
    f2 = {"sma_200": 90.0, "sma_50": 95.0, "sma_20": 101.0, "rsi_14": 50.0, "atr_14": 2.0}
    sigs2 = s.on_bar(make_bar(3, close=100.0, features=f2), FakePortfolio(holding=True, bars=10))
    assert sigs2 and sigs2[0].reason == "max_hold"


# --- Swing breakout ---

def test_breakout_enters_on_new_high_in_uptrend():
    s = SwingBreakout(lookback=5)
    pf = FakePortfolio()
    # Feed 5 prior bars (highs 100..104) to fill the rolling window — none should fire (no full window yet).
    for i in range(5):
        sigs = s.on_bar(make_bar(i + 1, close=100 + i, high=100 + i, features={"sma_200": 50.0}), pf)
        assert sigs == []
    # 6th bar closes above the prior-5 high (max 104) in an uptrend → breakout BUY
    sigs = s.on_bar(make_bar(6, close=106.0, high=106.0, features={"sma_200": 50.0}), pf)
    assert len(sigs) == 1 and sigs[0].side == Side.BUY and sigs[0].trailing_atr_mult == 3.0


def test_breakout_no_entry_below_prior_high_or_downtrend():
    s = SwingBreakout(lookback=3)
    pf = FakePortfolio()
    for i in range(3):
        s.on_bar(make_bar(i + 1, close=100 + i, high=100 + i, features={"sma_200": 50.0}), pf)
    # close below prior-3 high (102)
    assert s.on_bar(make_bar(4, close=101.0, high=101.0, features={"sma_200": 50.0}), pf) == []
    # new high but below sma_200 (downtrend)
    assert s.on_bar(make_bar(5, close=110.0, high=110.0, features={"sma_200": 200.0}), pf) == []


def test_breakout_exits_on_max_hold():
    s = SwingBreakout(lookback=3, max_hold=8)
    pf = FakePortfolio(holding=True, bars=8)
    sigs = s.on_bar(make_bar(1, close=100.0, high=100.0, features={"sma_200": 50.0}), pf)
    assert sigs and sigs[0].side == Side.SELL and sigs[0].reason == "max_hold"
