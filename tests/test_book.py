"""Capital-sleeve book: equity-curve summation, weight validation, blended metrics."""
from datetime import date, datetime

import pytest

from trading_system.analytics.book import Sleeve, _sum_equity_curves, combine_sleeves
from trading_system.portfolio.portfolio import EquityPoint


def _pt(day: int, equity: float) -> EquityPoint:
    return EquityPoint(
        ts=datetime(2024, 1, day), equity=equity,
        cash=equity, positions_value=0.0, drawdown=0.0,
    )


def test_sum_equity_curves_aligned_timestamps():
    a = [_pt(1, 700_000), _pt(2, 770_000), _pt(3, 735_000)]
    b = [_pt(1, 300_000), _pt(2, 303_000), _pt(3, 306_000)]
    summed = _sum_equity_curves([a, b], initials=[700_000, 300_000])
    assert [p.equity for p in summed] == [1_000_000, 1_073_000, 1_041_000]


def test_sum_equity_curves_forward_fill_missing_ts():
    """A sleeve missing a timestamp carries its last-known equity (forward-fill)."""
    a = [_pt(1, 700_000), _pt(2, 770_000), _pt(3, 700_000)]
    b = [_pt(1, 300_000), _pt(3, 330_000)]  # no day-2 snapshot
    summed = _sum_equity_curves([a, b], initials=[700_000, 300_000])
    # Day 2: a=770k, b forward-filled at 300k → 1,070k
    assert summed[1].equity == 1_070_000
    assert summed[2].equity == 1_030_000


def test_sum_equity_curves_recomputes_drawdown_on_blended_path():
    a = [_pt(1, 700_000), _pt(2, 700_000), _pt(3, 700_000)]
    b = [_pt(1, 300_000), _pt(2, 360_000), _pt(3, 300_000)]
    summed = _sum_equity_curves([a, b], initials=[700_000, 300_000])
    # Peak at day 2 (1,060k), trough day 3 (1,000k) → dd = -60k/1060k
    assert summed[2].drawdown == pytest.approx((1_000_000 - 1_060_000) / 1_060_000)


def test_combine_sleeves_rejects_weights_not_summing_to_one():
    with pytest.raises(ValueError, match="sum to 1.0"):
        combine_sleeves(
            [Sleeve("momentum", 0.5), Sleeve("momentum", 0.3)],
            bars=[], symbols=["X"], interval="day",
            from_date=date(2024, 1, 1), to_date=date(2024, 12, 31),
            total_capital=1_000_000, slippage_bps=0.0,
            fees_enabled=False, feature_set_version="v2",
        )


def test_combine_sleeves_rejects_nonpositive_weight():
    with pytest.raises(ValueError, match="positive"):
        combine_sleeves(
            [Sleeve("momentum", 1.0), Sleeve("swing_pullback", 0.0)],
            bars=[], symbols=["X"], interval="day",
            from_date=date(2024, 1, 1), to_date=date(2024, 12, 31),
            total_capital=1_000_000, slippage_bps=0.0,
            fees_enabled=False, feature_set_version="v2",
        )


def test_combine_sleeves_rejects_empty():
    with pytest.raises(ValueError, match="at least one"):
        combine_sleeves(
            [], bars=[], symbols=["X"], interval="day",
            from_date=date(2024, 1, 1), to_date=date(2024, 12, 31),
            total_capital=1_000_000, slippage_bps=0.0,
            fees_enabled=False, feature_set_version="v2",
        )


def test_sum_equity_curves_empty():
    assert _sum_equity_curves([], initials=[]) == []
