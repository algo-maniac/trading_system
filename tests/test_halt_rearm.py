"""Drawdown-halt re-arm: rolling-window peak lets entries resume after a crash ages out;
window=0 preserves the legacy all-time-peak behavior (halted forever once breached)."""
from datetime import date, datetime, timezone

from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.results import BacktestConfig
from trading_system.replay.events import BarEvent
from trading_system.risk.models import RiskConfig
from trading_system.strategy.base import PortfolioView, Strategy
from trading_system.strategy.signals import Side, Signal


class BuyWhenFlat(Strategy):
    """Always tries to be fully invested; dumps the position on a crash bar (close<=75)."""
    name = "buy_when_flat"
    version = "t"
    interval = "day"

    def on_bar(self, bar: BarEvent, portfolio: PortfolioView) -> list[Signal]:
        if portfolio.has_position(bar.symbol):
            if bar.close <= 75.0:
                return [Signal(symbol=bar.symbol, side=Side.SELL, reason="crash_exit")]
            return []
        return [Signal(symbol=bar.symbol, side=Side.BUY, reason="flat_reentry", size_hint=1.0)]


def _bar(day: int, close: float) -> BarEvent:
    return BarEvent(
        ts=datetime(2024, 1, day, tzinfo=timezone.utc), symbol="TEST", interval="day",
        instrument_token=1, open=close, high=close, low=close, close=close,
        volume=100_000, features={},
    )


def _run(window_days: int, n_days: int = 8):
    # Path: d1-d2 @100 (buy fills d2), d3 crash to 70 (dd -30% → halt; strategy exits),
    # d4 onward flat @70 while the strategy keeps trying to re-enter.
    closes = {1: 100.0, 2: 100.0, 3: 70.0}
    bars = [_bar(d, closes.get(d, 70.0)) for d in range(1, n_days + 1)]
    cfg = BacktestConfig(
        strategy_name="buy_when_flat", strategy_version="t", params={}, symbols=["TEST"],
        interval="day", from_date=date(2024, 1, 1), to_date=date(2024, 1, n_days),
        initial_capital=1_000_000, slippage_bps=0.0, fees_enabled=False,
        feature_set_version="v2",
    )
    risk = RiskConfig(
        max_position_pct=1.0, max_gross_exposure_pct=1.0, daily_loss_limit_pct=0.99,
        max_drawdown_pct=0.20, halt_peak_window_days=window_days,
    )
    engine = BacktestEngine(BuyWhenFlat(), cfg, risk_config=risk)
    result = engine.run(iter(bars))
    return result


def _buy_audits(result):
    return [(s.ts.day, s.risk_action) for s in result.signals if s.side == "BUY"]


def test_rolling_window_rearms_entries_after_crash_ages_out():
    result = _run(window_days=3)
    audits = dict(_buy_audits(result))
    assert audits[1] == "APPROVE"          # initial entry
    assert audits[4] == "REJECT"           # flat after crash-exit; peak(d2..d4)=1M → dd -30%
    assert audits[5] == "APPROVE"          # window now [d3,d4,d5] → peak 700k → dd 0 → re-armed
    # and the re-entry actually happens (position re-opened via d6 fill → no further BUY tries)
    assert 6 not in audits or audits[6] == "APPROVE"


def test_window_zero_keeps_legacy_all_time_halt():
    result = _run(window_days=0)
    audits = dict(_buy_audits(result))
    assert audits[1] == "APPROVE"
    # halted forever: every re-entry attempt after the crash is rejected
    for day in (4, 5, 6, 7, 8):
        assert audits[day] == "REJECT", f"day {day} should stay halted with window=0"
