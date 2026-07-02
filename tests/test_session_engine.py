"""Session engine: intraday EOD square-off, no overnight carry, late-entry block, MIS charges."""
from datetime import date, datetime

from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.results import BacktestConfig
from trading_system.execution.fees import Product
from trading_system.replay.events import BarEvent
from trading_system.strategy.vwap_reversion import VWAPReversion

# Features that satisfy VWAPReversion's entry (stretched below VWAP, range regime, oversold).
# vwap_dist ≈ -9% (close 100 vs vwap 110) clears the min_edge_pct cost gate.
_ENTRY = {"vwap": 110.0, "vwap_dist": -0.09, "vwap_z": -2.0, "adx_14": 20.0, "rsi_14": 30.0, "bar_in_session": 40.0, "atr_14": 1.0}
_HOLD = {**_ENTRY, "bar_in_session": 41.0}
_NOENTRY = {**_ENTRY, "vwap_z": 0.0}  # not stretched → no entry trigger


def _bar(hh, mm, close, feats, sym="RELIANCE", day=2):
    return BarEvent(
        ts=datetime(2024, 1, day, hh, mm), symbol=sym, interval="5minute", instrument_token=1,
        open=close, high=close + 0.5, low=close - 0.5, close=close, volume=1000, features=feats,
    )


def _cfg(product="MIS"):
    return BacktestConfig(
        strategy_name="vwap_reversion", strategy_version="v1", params={}, symbols=["RELIANCE"],
        interval="5minute", from_date=date(2024, 1, 2), to_date=date(2024, 1, 2),
        initial_capital=1_000_000, slippage_bps=0.0, fees_enabled=True,
        feature_set_version="intraday_v1", product=product,
    )


def _squareoff_stream():
    # Enter at 15:05 (before cutoff), never reverts → engine must flatten before the close.
    return [
        _bar(15, 5, 100.0, _ENTRY),
        _bar(15, 10, 100.0, _HOLD),   # entry fills @100; still below VWAP
        _bar(15, 15, 101.0, _HOLD),   # >= square_off_time → square-off SELL queued
        _bar(15, 20, 101.0, _HOLD),   # square-off fills @101 open
        _bar(15, 25, 101.0, _HOLD),
    ]


def test_position_flat_by_eod_via_squareoff():
    eng = BacktestEngine(VWAPReversion(), _cfg(), product=Product.MIS)
    res = eng.run(_squareoff_stream())
    assert eng.portfolio.open_symbols() == []          # nothing carried overnight
    assert len(res.trades) == 1
    assert res.trades[0].exit_reason == "eod_squareoff"


def test_no_entry_after_cutoff():
    # The only entry-worthy bar is at 15:20 (>= no_entry_after) → engine suppresses it.
    stream = [_bar(15, 5, 100.0, _NOENTRY), _bar(15, 20, 100.0, _ENTRY), _bar(15, 25, 100.0, _HOLD)]
    eng = BacktestEngine(VWAPReversion(), _cfg(), product=Product.MIS)
    res = eng.run(stream)
    assert res.trades == []
    assert eng.portfolio.open_symbols() == []


def test_mis_charges_differ_from_cnc():
    mis = BacktestEngine(VWAPReversion(), _cfg("MIS"), product=Product.MIS).run(_squareoff_stream())
    cnc = BacktestEngine(VWAPReversion(), _cfg("CNC"), product=Product.CNC).run(_squareoff_stream())
    assert mis.trades and cnc.trades
    # MIS (capped brokerage, STT sell-only) vs CNC (0.1% STT both sides) ⇒ different total charges.
    assert mis.trades[0].fees != cnc.trades[0].fees


def test_squareoff_determinism():
    r1 = BacktestEngine(VWAPReversion(), _cfg(), product=Product.MIS).run(_squareoff_stream())
    r2 = BacktestEngine(VWAPReversion(), _cfg(), product=Product.MIS).run(_squareoff_stream())
    assert [(t.entry_price, t.exit_price, t.pnl, t.exit_reason) for t in r1.trades] == \
           [(t.entry_price, t.exit_price, t.pnl, t.exit_reason) for t in r2.trades]
    assert r1.metrics == r2.metrics
