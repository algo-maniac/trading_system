"""Execution simulator tests: slippage direction + fees applied."""
from datetime import datetime, timezone

import pytest

from trading_system.execution.fees import Product
from trading_system.execution.simulator import simulate_fill

_TS = datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_buy_slippage_increases_price():
    fill = simulate_fill(_TS, "A", is_buy=True, qty=100, clean_price=1000.0,
                         slippage_bps=10.0, fees_enabled=False)
    assert fill.price == pytest.approx(1000.0 * 1.001)   # +10 bps
    assert fill.side == "BUY"


def test_sell_slippage_decreases_price():
    fill = simulate_fill(_TS, "A", is_buy=False, qty=100, clean_price=1000.0,
                         slippage_bps=10.0, fees_enabled=False)
    assert fill.price == pytest.approx(1000.0 * 0.999)   # -10 bps


def test_slippage_cost_recorded():
    fill = simulate_fill(_TS, "A", is_buy=True, qty=100, clean_price=1000.0,
                         slippage_bps=10.0, fees_enabled=False)
    assert fill.slippage == pytest.approx(abs(fill.price - 1000.0) * 100)


def test_fees_applied_when_enabled():
    fill = simulate_fill(_TS, "A", is_buy=True, qty=100, clean_price=1000.0,
                         slippage_bps=0.0, fees_enabled=True, product=Product.CNC)
    assert fill.fees > 0


def test_fees_zero_when_disabled():
    fill = simulate_fill(_TS, "A", is_buy=True, qty=100, clean_price=1000.0,
                         slippage_bps=0.0, fees_enabled=False)
    assert fill.fees == 0.0


def test_zero_slippage_keeps_clean_price():
    fill = simulate_fill(_TS, "A", is_buy=True, qty=100, clean_price=1000.0,
                         slippage_bps=0.0, fees_enabled=False)
    assert fill.price == pytest.approx(1000.0)
    assert fill.slippage == 0.0
