"""Portfolio accounting tests."""
from datetime import datetime, timezone

import pytest

from trading_system.portfolio.models import Fill
from trading_system.portfolio.portfolio import Portfolio

_UTC = timezone.utc


def _ts(day: int = 1) -> datetime:
    return datetime(2024, 1, day, tzinfo=_UTC)


def test_buy_reduces_cash_and_opens_position():
    p = Portfolio(1_000_000)
    p.apply_fill(Fill(_ts(), "RELIANCE", "BUY", 100, 2000.0, fees=50.0, slippage=10.0))
    assert p.cash == pytest.approx(1_000_000 - 100 * 2000.0 - 50.0)
    assert p.has_position("RELIANCE")
    assert p.position_qty("RELIANCE") == 100
    assert p.entry_price("RELIANCE") == pytest.approx(2000.0)


def test_sell_closes_position_and_realizes_pnl():
    p = Portfolio(1_000_000)
    p.apply_fill(Fill(_ts(1), "TCS", "BUY", 10, 3000.0, fees=30.0, slippage=5.0))
    trade = p.apply_fill(Fill(_ts(2), "TCS", "SELL", 10, 3300.0, fees=33.0, slippage=5.0), reason="target")

    assert trade is not None
    assert not p.has_position("TCS")
    # gross = 10*(3300-3000)=3000; net = 3000 - exit_fees(33) - entry_fees(30) = 2937
    assert trade.pnl == pytest.approx(2937.0)
    assert trade.exit_reason == "target"
    assert p.realized_pnl == pytest.approx(2937.0)


def test_position_averaging_on_add():
    p = Portfolio(1_000_000)
    p.apply_fill(Fill(_ts(1), "INFY", "BUY", 100, 1000.0, fees=10.0, slippage=0.0))
    p.apply_fill(Fill(_ts(2), "INFY", "BUY", 100, 1200.0, fees=10.0, slippage=0.0))
    assert p.position_qty("INFY") == 200
    assert p.entry_price("INFY") == pytest.approx(1100.0)  # (100*1000 + 100*1200)/200


def test_partial_sell_keeps_remaining():
    p = Portfolio(1_000_000)
    p.apply_fill(Fill(_ts(1), "SBIN", "BUY", 100, 500.0, fees=20.0, slippage=0.0))
    p.apply_fill(Fill(_ts(2), "SBIN", "SELL", 40, 550.0, fees=8.0, slippage=0.0), reason="partial")
    assert p.position_qty("SBIN") == 60


def test_equity_includes_unrealized():
    p = Portfolio(1_000_000)
    p.apply_fill(Fill(_ts(1), "WIPRO", "BUY", 100, 400.0, fees=0.0, slippage=0.0))
    p.update_price("WIPRO", 450.0)
    # cash = 1M - 40k = 960k; positions = 100*450 = 45k; equity = 1.005M
    assert p.equity() == pytest.approx(960_000 + 45_000)


def test_drawdown_tracked_in_snapshot():
    p = Portfolio(1_000_000)
    p.apply_fill(Fill(_ts(1), "X", "BUY", 1000, 1000.0, fees=0.0, slippage=0.0))
    p.update_price("X", 1000.0)
    p.snapshot(_ts(1))                  # equity ~1M, peak set
    p.update_price("X", 800.0)          # position drops 200k
    pt = p.snapshot(_ts(2))
    assert pt.drawdown < 0
    assert pt.equity == pytest.approx(p.cash + 1000 * 800.0)


def test_age_position_increments_bars_held():
    p = Portfolio(1_000_000)
    p.apply_fill(Fill(_ts(1), "Y", "BUY", 10, 100.0, fees=0.0, slippage=0.0))
    assert p.bars_held("Y") == 0
    p.age_position("Y")
    p.age_position("Y")
    assert p.bars_held("Y") == 2
