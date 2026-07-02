"""Backtest engine end-to-end tests on synthetic streams (no DB).

Headline check: running the same stream twice yields identical trades + metrics.
Also validates next-bar-open execution and that fees reduce returns.
"""
from datetime import date

import pytest

from tests.conftest import make_bar
from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.results import BacktestConfig
from trading_system.strategy.momentum import MomentumStrategy, MomentumV2, MomentumV3


def _stream():
    """A path that triggers one momentum round-trip (golden cross → death cross)."""
    return [
        make_bar(1, "AAA", close=100, open_=100, features={"sma_20": 95, "sma_50": 100, "momentum_20": 0.01}),
        make_bar(2, "AAA", close=102, open_=101, features={"sma_20": 105, "sma_50": 100, "momentum_20": 0.05}),
        make_bar(3, "AAA", close=103, open_=102, high=104, low=101, features={"sma_20": 106, "sma_50": 100, "momentum_20": 0.05}),
        make_bar(4, "AAA", close=104, open_=103, high=105, low=102, features={"sma_20": 95, "sma_50": 100, "momentum_20": -0.01}),
        make_bar(5, "AAA", close=104, open_=104, high=105, low=103, features={"sma_20": 95, "sma_50": 100, "momentum_20": -0.01}),
    ]


def _config(fees_enabled=True, slippage_bps=0.0):
    return BacktestConfig(
        strategy_name="momentum", strategy_version="v1", params={},
        symbols=["AAA"], interval="day", from_date=date(2024, 1, 1), to_date=date(2024, 1, 5),
        initial_capital=1_000_000, slippage_bps=slippage_bps, fees_enabled=fees_enabled,
        feature_set_version="v1",
    )


def test_engine_produces_one_round_trip():
    result = BacktestEngine(MomentumStrategy(), _config()).run(_stream())
    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.symbol == "AAA"
    assert "death_cross" in t.exit_reason


def test_next_bar_open_execution():
    # Signal fires on bar 2 (close); fill must be at bar 3's OPEN (102), not bar 2's close.
    result = BacktestEngine(MomentumStrategy(), _config(slippage_bps=0.0)).run(_stream())
    assert result.trades[0].entry_price == pytest.approx(102.0)   # bar 3 open
    assert result.trades[0].exit_price == pytest.approx(104.0)    # bar 5 open


def test_determinism_identical_runs():
    r1 = BacktestEngine(MomentumStrategy(), _config()).run(_stream())
    r2 = BacktestEngine(MomentumStrategy(), _config()).run(_stream())

    assert [(t.entry_price, t.exit_price, t.pnl, t.fees) for t in r1.trades] == \
           [(t.entry_price, t.exit_price, t.pnl, t.fees) for t in r2.trades]
    assert r1.metrics == r2.metrics
    assert [(p.ts, p.equity) for p in r1.equity_curve] == [(p.ts, p.equity) for p in r2.equity_curve]


def test_fees_reduce_returns():
    with_fees = BacktestEngine(MomentumStrategy(), _config(fees_enabled=True)).run(_stream())
    no_fees = BacktestEngine(MomentumStrategy(), _config(fees_enabled=False)).run(_stream())
    assert no_fees.metrics["final_equity"] > with_fees.metrics["final_equity"]


def test_config_hash_stable_and_sensitive():
    c1 = _config()
    c2 = _config()
    c3 = _config(fees_enabled=False)
    assert c1.config_hash() == c2.config_hash()    # same config → same hash
    assert c1.config_hash() != c3.config_hash()    # different config → different hash


def test_every_signal_audited():
    result = BacktestEngine(MomentumStrategy(), _config()).run(_stream())
    # At least the entry BUY and the exit SELL were recorded
    actions = [(s.side, s.risk_action) for s in result.signals]
    assert ("BUY", "APPROVE") in actions
    assert any(s.side == "SELL" for s in result.signals)


# --- Trailing stop (MomentumV2) ---

def _trailing_stream():
    """Enter on a regime-confirmed cross, rally, then pull back through the trailed stop."""
    base = {"sma_20": 120, "sma_50": 100, "momentum_20": 0.05, "sma_200": 80}
    return [
        # prime prev_above=False (sma_20 < sma_50)
        make_bar(1, "AAA", close=100, open_=100, features={**base, "sma_20": 95}),
        # fresh cross, close > sma_200 → BUY queued
        make_bar(2, "AAA", close=110, open_=108, features=base),
        # fill at open=110; rallies to high 130 → trailing stop ratchets to 130*0.88=114.4
        make_bar(3, "AAA", close=128, open_=110, high=130, low=109, features=base),
        # pulls back: low 110 pierces 114.4 → trailing-stop exit at 114.4 (still above entry)
        make_bar(4, "AAA", close=112, open_=120, high=121, low=110, features=base),
    ]


def _v2_config():
    return BacktestConfig(
        strategy_name="momentum_v2", strategy_version="v2", params={},
        symbols=["AAA"], interval="day", from_date=date(2024, 1, 1), to_date=date(2024, 1, 4),
        initial_capital=1_000_000, slippage_bps=0.0, fees_enabled=False,
        feature_set_version="v2",
    )


def test_trailing_stop_ratchets_and_locks_profit():
    result = BacktestEngine(MomentumV2(trailing_stop_pct=0.12), _v2_config()).run(_trailing_stream())
    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.exit_reason == "stop_loss"
    assert t.entry_price == pytest.approx(110.0)      # bar 3 open
    assert t.exit_price == pytest.approx(114.4)        # trailed to 130 * 0.88
    assert t.pnl > 0                                   # trailing stop locked in a profit


def test_trailing_stop_determinism():
    r1 = BacktestEngine(MomentumV2(), _v2_config()).run(_trailing_stream())
    r2 = BacktestEngine(MomentumV2(), _v2_config()).run(_trailing_stream())
    assert [(t.entry_price, t.exit_price, t.pnl) for t in r1.trades] == \
           [(t.entry_price, t.exit_price, t.pnl) for t in r2.trades]


# --- ATR-based trailing stop (MomentumV3) ---

def _atr_stream():
    """Enter on a regime cross with ATR-based stop; rally lifts the stop, then pullback exits."""
    base = {"sma_20": 120, "sma_50": 100, "momentum_20": 0.05, "sma_200": 80}
    return [
        # prime prev_above = False
        make_bar(1, "AAA", close=100, open_=100, features={**base, "sma_20": 95, "atr_14": 10}),
        # fresh cross + close > sma_200 → BUY queued (trailing_atr_mult=3.0)
        make_bar(2, "AAA", close=110, open_=108, features={**base, "atr_14": 10}),
        # fill at open=110; initial_stop = 110 − 3×10 = 80;
        # high=150 → ratchet: 150 − 30 = 120 > 80, stop becomes 120
        make_bar(3, "AAA", close=145, open_=110, high=150, low=109, features={**base, "atr_14": 10}),
        # pullback: low=100 < stop=120 → exit at min(open=130, stop=120) = 120
        make_bar(4, "AAA", close=115, open_=130, high=131, low=100, features={**base, "atr_14": 10}),
    ]


def _atr_stream_no_atr():
    """ATR absent → no stop seeded. Death cross on bar 3 queues SELL; fills on bar 4."""
    base = {"sma_20": 120, "sma_50": 100, "momentum_20": 0.05, "sma_200": 80}
    return [
        make_bar(1, "AAA", close=100, open_=100, features={**base, "sma_20": 95}),
        # fresh cross, no atr_14 in features → BUY queued
        make_bar(2, "AAA", close=110, open_=108, features=base),
        # fill at open=110; no ATR → no stop; death cross (sma_20 < sma_50) → SELL queued
        make_bar(3, "AAA", close=90, open_=110, high=111, low=85, features={**base, "sma_20": 95}),
        # SELL fills at open=90 → trade recorded; exit_reason is death_cross, not stop_loss
        make_bar(4, "AAA", close=85, open_=90, features={**base, "sma_20": 95}),
    ]


def _v3_config():
    return BacktestConfig(
        strategy_name="momentum_v3", strategy_version="v3", params={},
        symbols=["AAA"], interval="day", from_date=date(2024, 1, 1), to_date=date(2024, 1, 4),
        initial_capital=1_000_000, slippage_bps=0.0, fees_enabled=False,
        feature_set_version="v2",
    )


def test_atr_trailing_stop_ratchets_and_locks_profit():
    result = BacktestEngine(MomentumV3(trailing_atr_mult=3.0), _v3_config()).run(_atr_stream())
    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.exit_reason == "stop_loss"
    assert t.entry_price == pytest.approx(110.0)   # bar 3 open
    assert t.exit_price == pytest.approx(120.0)    # stop ratcheted to 150 − 3×10 = 120
    assert t.pnl > 0                               # trailing stop locked in profit


def test_atr_trailing_stop_determinism():
    r1 = BacktestEngine(MomentumV3(), _v3_config()).run(_atr_stream())
    r2 = BacktestEngine(MomentumV3(), _v3_config()).run(_atr_stream())
    assert [(t.entry_price, t.exit_price, t.pnl) for t in r1.trades] == \
           [(t.entry_price, t.exit_price, t.pnl) for t in r2.trades]


def test_atr_stop_missing_atr_no_crash():
    """When atr_14 is absent, no stop is seeded — engine runs without crashing."""
    result = BacktestEngine(MomentumV3(trailing_atr_mult=3.0), _v3_config()).run(_atr_stream_no_atr())
    # Position exits via death_cross SELL (no ATR stop available to trigger first)
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason != "stop_loss"
