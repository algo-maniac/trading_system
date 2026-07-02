"""Analytics metrics tests against known fixtures."""
from datetime import datetime, timedelta, timezone

import pytest

from trading_system.analytics.metrics import max_drawdown, sharpe_ratio
from trading_system.analytics.metrics import compute_metrics
from trading_system.portfolio.models import Trade
from trading_system.portfolio.portfolio import EquityPoint

_UTC = timezone.utc


def test_max_drawdown_simple():
    equity = [100, 110, 88, 99, 132, 105]
    # peaks: 100,110,110,110,132,132. Drawdowns: 88/110=-0.20 and 105/132=-0.2045.
    # Worst is the final point: (105-132)/132.
    assert max_drawdown(equity) == pytest.approx(-27 / 132, abs=1e-9)


def test_max_drawdown_no_drop():
    assert max_drawdown([100, 110, 120]) == pytest.approx(0.0)


def test_sharpe_zero_when_constant():
    assert sharpe_ratio([0.0, 0.0, 0.0], 252) == 0.0


def test_sharpe_positive_for_positive_returns():
    rets = [0.01, 0.012, 0.009, 0.011]
    assert sharpe_ratio(rets, 252) > 0


def _eq_curve(values):
    base = datetime(2024, 1, 1, tzinfo=_UTC)
    return [
        EquityPoint(ts=base + timedelta(days=i), equity=v, cash=v, positions_value=0.0, drawdown=0.0)
        for i, v in enumerate(values)
    ]


def _trade(pnl):
    base = datetime(2024, 1, 1, tzinfo=_UTC)
    return Trade("A", "LONG", base, 100.0, base + timedelta(days=1), 100 + pnl / 10,
                 qty=10, fees=0.0, slippage=0.0, pnl=pnl, return_pct=pnl / 1000, exit_reason="x")


def test_profit_factor_and_win_rate():
    trades = [_trade(300), _trade(200), _trade(-100), _trade(-150)]
    m = compute_metrics(_eq_curve([1000, 1100, 1250]), trades, "day", 1000)
    assert m["profit_factor"] == pytest.approx(500 / 250)   # gross profit 500 / gross loss 250
    assert m["win_rate"] == pytest.approx(0.5)
    assert m["num_trades"] == 4
    assert m["expectancy"] == pytest.approx((300 + 200 - 100 - 150) / 4)


def test_total_return_computed():
    m = compute_metrics(_eq_curve([1000, 1100, 1200]), [], "day", 1000)
    assert m["total_return"] == pytest.approx(0.20)


def test_profit_factor_infinite_when_no_losses():
    trades = [_trade(100), _trade(50)]
    m = compute_metrics(_eq_curve([1000, 1150]), trades, "day", 1000)
    assert m["profit_factor"] == float("inf")
