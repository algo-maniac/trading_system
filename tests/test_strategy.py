"""Reference strategy signal tests on crafted feature data."""
import pytest

from tests.conftest import make_bar
from trading_system.strategy.mean_reversion import MeanReversionStrategy, MeanReversionV2
from trading_system.strategy.momentum import MomentumStrategy, MomentumV2, MomentumV3
from trading_system.strategy.signals import Side


class FakePortfolio:
    """Minimal PortfolioView stub for strategy unit tests."""
    def __init__(self, holding=False, bars=0):
        self._holding = holding
        self._bars = bars

    def has_position(self, symbol): return self._holding
    def position_qty(self, symbol): return 100 if self._holding else 0
    def entry_price(self, symbol): return 100.0
    def bars_held(self, symbol): return self._bars


# --- Momentum ---

def test_momentum_no_signal_on_warmup():
    s = MomentumStrategy()
    bar = make_bar(1, features={})  # no sma features
    assert s.on_bar(bar, FakePortfolio()) == []


def test_momentum_enters_on_fresh_golden_cross():
    s = MomentumStrategy()
    pf = FakePortfolio(holding=False)
    # Bar 1: sma_20 below sma_50 → sets prev_above = False
    s.on_bar(make_bar(1, features={"sma_20": 95, "sma_50": 100, "momentum_20": 0.01}), pf)
    # Bar 2: sma_20 now above sma_50 + positive momentum → fresh cross → BUY
    sigs = s.on_bar(make_bar(2, features={"sma_20": 105, "sma_50": 100, "momentum_20": 0.05}), pf)
    assert len(sigs) == 1
    assert sigs[0].side == Side.BUY
    assert sigs[0].stop_pct == 0.08


def test_momentum_no_entry_without_fresh_cross():
    s = MomentumStrategy()
    pf = FakePortfolio(holding=False)
    # First bar already above — prev_above unknown (None), so not a *fresh* cross
    sigs = s.on_bar(make_bar(1, features={"sma_20": 105, "sma_50": 100, "momentum_20": 0.05}), pf)
    assert sigs == []


def test_momentum_no_entry_when_momentum_negative():
    s = MomentumStrategy()
    pf = FakePortfolio(holding=False)
    s.on_bar(make_bar(1, features={"sma_20": 95, "sma_50": 100, "momentum_20": -0.01}), pf)
    sigs = s.on_bar(make_bar(2, features={"sma_20": 105, "sma_50": 100, "momentum_20": -0.02}), pf)
    assert sigs == []


def test_momentum_exits_on_death_cross():
    s = MomentumStrategy()
    pf = FakePortfolio(holding=True)
    sigs = s.on_bar(make_bar(3, features={"sma_20": 95, "sma_50": 100, "momentum_20": -0.01}), pf)
    assert len(sigs) == 1
    assert sigs[0].side == Side.SELL


# --- Mean reversion ---

def test_mean_reversion_enters_when_oversold():
    s = MeanReversionStrategy()
    pf = FakePortfolio(holding=False)
    sigs = s.on_bar(make_bar(1, close=90, features={"rsi_14": 25, "sma_20": 100}), pf)
    assert len(sigs) == 1
    assert sigs[0].side == Side.BUY
    assert sigs[0].stop_pct == 0.05


def test_mean_reversion_no_entry_when_not_oversold():
    s = MeanReversionStrategy()
    pf = FakePortfolio(holding=False)
    sigs = s.on_bar(make_bar(1, close=90, features={"rsi_14": 45, "sma_20": 100}), pf)
    assert sigs == []


def test_mean_reversion_exits_on_reversion():
    s = MeanReversionStrategy()
    pf = FakePortfolio(holding=True)
    sigs = s.on_bar(make_bar(2, close=98, features={"rsi_14": 55, "sma_20": 100}), pf)
    assert len(sigs) == 1
    assert sigs[0].side == Side.SELL
    assert "reverted" in sigs[0].reason


def test_mean_reversion_exits_at_mean():
    s = MeanReversionStrategy()
    pf = FakePortfolio(holding=True)
    sigs = s.on_bar(make_bar(2, close=101, features={"rsi_14": 40, "sma_20": 100}), pf)
    assert sigs[0].side == Side.SELL
    assert "mean_reached" in sigs[0].reason


def test_mean_reversion_time_stop():
    s = MeanReversionStrategy(max_hold_bars=10)
    pf = FakePortfolio(holding=True, bars=10)
    sigs = s.on_bar(make_bar(2, close=95, features={"rsi_14": 40, "sma_20": 100}), pf)
    assert sigs[0].side == Side.SELL
    assert "time_stop" in sigs[0].reason


# --- Momentum v2 (regime filter + trailing stop) ---

def _cross(close, sma_200):
    """A fresh-golden-cross feature dict for momentum_v2, given close & sma_200."""
    return {"sma_20": 105, "sma_50": 100, "momentum_20": 0.05, "sma_200": sma_200}


def test_momentum_v2_regime_blocks_entry_below_sma200():
    s = MomentumV2()
    pf = FakePortfolio(holding=False)
    # prime prev_above = False (sma_20 below sma_50)
    s.on_bar(make_bar(1, close=98, features={"sma_20": 95, "sma_50": 100, "momentum_20": 0.01, "sma_200": 120}), pf)
    # fresh cross + positive momentum, but close (98) < sma_200 (120) → regime blocks entry
    sigs = s.on_bar(make_bar(2, close=98, features=_cross(98, 120)), pf)
    assert sigs == []


def test_momentum_v2_enters_with_regime_and_uses_trailing_stop():
    s = MomentumV2(trailing_stop_pct=0.12)
    pf = FakePortfolio(holding=False)
    s.on_bar(make_bar(1, close=98, features={"sma_20": 95, "sma_50": 100, "momentum_20": 0.01, "sma_200": 90}), pf)
    # fresh cross + close (110) > sma_200 (90) → BUY with trailing stop, no fixed stop
    sigs = s.on_bar(make_bar(2, close=110, features=_cross(110, 90)), pf)
    assert len(sigs) == 1
    assert sigs[0].side == Side.BUY
    assert sigs[0].trailing_stop_pct == 0.12
    assert sigs[0].stop_pct is None


def test_momentum_v2_warmup_requires_sma200():
    s = MomentumV2()
    pf = FakePortfolio(holding=False)
    # sma_200 missing → warmup, no signal even on a cross
    s.on_bar(make_bar(1, features={"sma_20": 95, "sma_50": 100, "momentum_20": 0.01}), pf)
    assert s.on_bar(make_bar(2, features={"sma_20": 105, "sma_50": 100, "momentum_20": 0.05}), pf) == []


# --- Mean reversion v2 (stricter entry + min-hold) ---

def test_mean_reversion_v2_stricter_entry():
    s = MeanReversionV2()  # rsi_entry=20
    pf = FakePortfolio(holding=False)
    # rsi 25 would enter v1 but NOT v2 (needs < 20)
    assert s.on_bar(make_bar(1, close=90, features={"rsi_14": 25, "sma_20": 100}), pf) == []
    sigs = s.on_bar(make_bar(2, close=90, features={"rsi_14": 15, "sma_20": 100}), pf)
    assert len(sigs) == 1 and sigs[0].side == Side.BUY


def test_mean_reversion_v2_min_hold_blocks_early_exit():
    s = MeanReversionV2(min_hold_bars=3)
    # held only 1 bar; reverted, but min-hold blocks the soft exit
    pf_early = FakePortfolio(holding=True, bars=1)
    assert s.on_bar(make_bar(2, close=102, features={"rsi_14": 60, "sma_20": 100}), pf_early) == []
    # held 3 bars → soft exit allowed
    pf_ok = FakePortfolio(holding=True, bars=3)
    sigs = s.on_bar(make_bar(2, close=102, features={"rsi_14": 60, "sma_20": 100}), pf_ok)
    assert len(sigs) == 1 and sigs[0].side == Side.SELL


def test_mean_reversion_v2_time_stop_overrides_min_hold():
    s = MeanReversionV2(min_hold_bars=3, max_hold_bars=10)
    pf = FakePortfolio(holding=True, bars=10)
    sigs = s.on_bar(make_bar(2, close=95, features={"rsi_14": 40, "sma_20": 100}), pf)
    assert sigs[0].side == Side.SELL and "time_stop" in sigs[0].reason


# --- Momentum v3 (regime filter + ATR-based trailing stop) ---

def _v3_cross(close, sma_200):
    return {"sma_20": 105, "sma_50": 100, "momentum_20": 0.05, "sma_200": sma_200}


def test_momentum_v3_emits_trailing_atr_mult():
    s = MomentumV3(trailing_atr_mult=3.0)
    pf = FakePortfolio(holding=False)
    # prime prev_above = False
    s.on_bar(make_bar(1, close=98, features={"sma_20": 95, "sma_50": 100, "momentum_20": 0.01, "sma_200": 90}), pf)
    # fresh cross + close > sma_200 → BUY with trailing_atr_mult, no pct stop
    sigs = s.on_bar(make_bar(2, close=110, features=_v3_cross(110, 90)), pf)
    assert len(sigs) == 1
    assert sigs[0].side == Side.BUY
    assert sigs[0].trailing_atr_mult == 3.0
    assert sigs[0].trailing_stop_pct is None
    assert sigs[0].stop_pct is None


def test_momentum_v3_regime_blocks_entry_below_sma200():
    s = MomentumV3()
    pf = FakePortfolio(holding=False)
    s.on_bar(make_bar(1, close=98, features={"sma_20": 95, "sma_50": 100, "momentum_20": 0.01, "sma_200": 120}), pf)
    # close (98) < sma_200 (120) → regime blocks entry
    sigs = s.on_bar(make_bar(2, close=98, features=_v3_cross(98, 120)), pf)
    assert sigs == []


def test_momentum_v3_missing_atr_still_enters():
    """atr_14 absent does not block entry — ATR stop seeds lazily in the engine."""
    s = MomentumV3()
    pf = FakePortfolio(holding=False)
    s.on_bar(make_bar(1, close=98, features={"sma_20": 95, "sma_50": 100, "momentum_20": 0.01, "sma_200": 90}), pf)
    # No atr_14 — strategy still emits BUY (engine handles the missing ATR gracefully)
    sigs = s.on_bar(make_bar(2, close=110, features={"sma_20": 105, "sma_50": 100, "momentum_20": 0.05, "sma_200": 90}), pf)
    assert len(sigs) == 1
    assert sigs[0].side == Side.BUY
    assert sigs[0].trailing_atr_mult == 3.0


def test_momentum_v3_warmup_requires_sma200():
    s = MomentumV3()
    pf = FakePortfolio(holding=False)
    s.on_bar(make_bar(1, features={"sma_20": 95, "sma_50": 100, "momentum_20": 0.01}), pf)
    # sma_200 missing → warmup, no signal
    assert s.on_bar(make_bar(2, features={"sma_20": 105, "sma_50": 100, "momentum_20": 0.05}), pf) == []
