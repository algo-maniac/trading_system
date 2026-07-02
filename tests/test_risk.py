"""Risk manager + rule tests."""
from datetime import datetime, timezone

import pytest

from trading_system.portfolio.models import Fill
from trading_system.portfolio.portfolio import Portfolio
from trading_system.risk.manager import RiskManager
from trading_system.risk.models import RiskAction, RiskConfig
from trading_system.strategy.signals import Side, Signal

_UTC = timezone.utc
_TS = datetime(2024, 1, 1, tzinfo=_UTC)


def _buy(symbol="RELIANCE", size_hint=0.10, stop_pct=0.08):
    return Signal(symbol=symbol, side=Side.BUY, reason="t", size_hint=size_hint, stop_pct=stop_pct)


def test_approve_within_limits():
    p = Portfolio(1_000_000)
    rm = RiskManager(RiskConfig(max_position_pct=0.10))
    d = rm.evaluate(_buy(size_hint=0.10), p, ref_price=1000.0)
    # 10% of 1M = 100k / 1000 = 100 shares, within position cap
    assert d.action == RiskAction.APPROVE
    assert d.approved_qty == 100


def test_resize_when_size_hint_exceeds_position_cap():
    p = Portfolio(1_000_000)
    rm = RiskManager(RiskConfig(max_position_pct=0.05))   # cap 5%
    d = rm.evaluate(_buy(size_hint=0.20), p, ref_price=1000.0)  # wants 20%
    assert d.action == RiskAction.RESIZE
    assert d.approved_qty == 50                            # 5% of 1M / 1000


def test_reject_when_max_positions_reached():
    p = Portfolio(1_000_000)
    # open 2 positions
    p.apply_fill(Fill(_TS, "A", "BUY", 1, 100.0, 0, 0))
    p.apply_fill(Fill(_TS, "B", "BUY", 1, 100.0, 0, 0))
    rm = RiskManager(RiskConfig(max_positions=2))
    d = rm.evaluate(_buy(symbol="C"), p, ref_price=1000.0)
    assert d.action == RiskAction.REJECT
    assert "max_positions" in d.reason


def test_adding_to_existing_position_not_blocked_by_max_positions():
    p = Portfolio(1_000_000)
    p.apply_fill(Fill(_TS, "A", "BUY", 1, 100.0, 0, 0))
    rm = RiskManager(RiskConfig(max_positions=1, max_position_pct=1.0, max_gross_exposure_pct=1.0))
    d = rm.evaluate(_buy(symbol="A"), p, ref_price=1000.0)
    assert d.action in (RiskAction.APPROVE, RiskAction.RESIZE)  # not rejected for count


def test_gross_exposure_cap():
    p = Portfolio(1_000_000)
    # already 95% exposed
    p.apply_fill(Fill(_TS, "A", "BUY", 950, 1000.0, 0, 0))
    p.update_price("A", 1000.0)
    rm = RiskManager(RiskConfig(max_gross_exposure_pct=1.0, max_position_pct=1.0))
    d = rm.evaluate(_buy(symbol="B", size_hint=0.10), p, ref_price=1000.0)
    # only ~5% budget left → 50 shares max, wants 100 → resize
    assert d.action == RiskAction.RESIZE
    assert d.approved_qty <= 50


def test_daily_loss_limit_halts_entries():
    p = Portfolio(1_000_000)
    rm = RiskManager(RiskConfig(daily_loss_limit_pct=0.05))
    d = rm.evaluate(_buy(), p, ref_price=1000.0, day_return_pct=-0.06)
    assert d.action == RiskAction.REJECT
    assert "daily_loss_limit" in d.reason


def test_max_drawdown_halts_entries():
    p = Portfolio(1_000_000)
    rm = RiskManager(RiskConfig(max_drawdown_pct=0.20))
    d = rm.evaluate(_buy(), p, ref_price=1000.0, current_drawdown=-0.25)
    assert d.action == RiskAction.REJECT
    assert "max_drawdown" in d.reason


def test_sell_always_approved_for_full_position():
    p = Portfolio(1_000_000)
    p.apply_fill(Fill(_TS, "A", "BUY", 100, 1000.0, 0, 0))
    rm = RiskManager()
    d = rm.evaluate(Signal("A", Side.SELL, "exit"), p, ref_price=1000.0, current_drawdown=-0.99)
    assert d.action == RiskAction.APPROVE       # exits never blocked
    assert d.approved_qty == 100


def test_sell_with_no_position_rejected():
    p = Portfolio(1_000_000)
    rm = RiskManager()
    d = rm.evaluate(Signal("A", Side.SELL, "exit"), p, ref_price=1000.0)
    assert d.action == RiskAction.REJECT
