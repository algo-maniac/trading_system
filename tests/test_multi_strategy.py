"""MultiStrategy: signal fan-out, SELL precedence, portfolio isolation, validation."""
import pytest

from tests.conftest import make_bar
from trading_system.strategy.base import Strategy
from trading_system.strategy.multi import MultiStrategy
from trading_system.strategy.signals import Side, Signal


class _FakePortfolio:
    def __init__(self, holding: bool = False):
        self._holding = holding

    def has_position(self, symbol: str) -> bool:
        return self._holding

    def position_qty(self, symbol: str) -> int:
        return 100 if self._holding else 0

    def entry_price(self, symbol: str) -> float:
        return 100.0

    def bars_held(self, symbol: str) -> int:
        return 0

    def open_symbols(self) -> list[str]:
        return ["TEST"] if self._holding else []


# --- Minimal stub strategies for structural tests ---

class _AlwaysBuy(Strategy):
    name = "always_buy"
    version = "v1"
    interval = "day"

    def on_bar(self, bar, portfolio) -> list[Signal]:
        if not portfolio.has_position(bar.symbol):
            return [Signal(symbol=bar.symbol, side=Side.BUY, reason="buy")]
        return []


class _AlwaysSell(Strategy):
    name = "always_sell"
    version = "v1"
    interval = "day"

    def on_bar(self, bar, portfolio) -> list[Signal]:
        return [Signal(symbol=bar.symbol, side=Side.SELL, reason="sell")]


class _AlwaysBuy2(Strategy):
    name = "also_buy"
    version = "v1"
    interval = "day"

    def on_bar(self, bar, portfolio) -> list[Signal]:
        if not portfolio.has_position(bar.symbol):
            return [Signal(symbol=bar.symbol, side=Side.BUY, reason="also_buy")]
        return []


class _IntradayStrat(Strategy):
    name = "intra"
    version = "v1"
    interval = "5minute"
    intraday = True

    def on_bar(self, bar, portfolio) -> list[Signal]:
        return []


# -----------------------------------------------------------------------

def test_multi_fans_out_to_both_strategies():
    """Both sub-strategies' signals for different symbols are returned."""
    class _BuyA(Strategy):
        name = "buy_a"
        version = "v1"
        interval = "day"
        def on_bar(self, bar, portfolio):
            return [Signal("A", Side.BUY, "a")] if bar.symbol == "A" else []

    class _BuyB(Strategy):
        name = "buy_b"
        version = "v1"
        interval = "day"
        def on_bar(self, bar, portfolio):
            return [Signal("B", Side.BUY, "b")] if bar.symbol == "B" else []

    multi = MultiStrategy([_BuyA(), _BuyB()])
    pf = _FakePortfolio()

    sigs_a = multi.on_bar(make_bar(1, symbol="A"), pf)
    assert len(sigs_a) == 1 and sigs_a[0].symbol == "A" and sigs_a[0].side == Side.BUY

    sigs_b = multi.on_bar(make_bar(1, symbol="B"), pf)
    assert len(sigs_b) == 1 and sigs_b[0].symbol == "B" and sigs_b[0].side == Side.BUY


def test_sell_takes_precedence_over_buy_same_symbol():
    """When one strategy says BUY and another says SELL for the same symbol, SELL wins."""
    multi = MultiStrategy([_AlwaysBuy(), _AlwaysSell()])
    sigs = multi.on_bar(make_bar(1), _FakePortfolio(holding=False))
    assert len(sigs) == 1
    assert sigs[0].side == Side.SELL


def test_two_buy_signals_for_same_symbol_deduplicates():
    """Two BUY signals for the same symbol collapse to one."""
    multi = MultiStrategy([_AlwaysBuy(), _AlwaysBuy2()])
    sigs = multi.on_bar(make_bar(1), _FakePortfolio(holding=False))
    assert len(sigs) == 1
    assert sigs[0].side == Side.BUY


def test_no_signals_when_both_strategies_silent():
    multi = MultiStrategy([_AlwaysBuy(), _AlwaysBuy2()])
    # Portfolio already has position → neither _AlwaysBuy nor _AlwaysBuy2 fires
    sigs = multi.on_bar(make_bar(1), _FakePortfolio(holding=True))
    assert sigs == []


def test_multi_strategy_name_is_concatenated():
    multi = MultiStrategy([_AlwaysBuy(), _AlwaysSell()])
    assert multi.name == "always_buy+always_sell"
    assert multi.version == "v1+v1"


def test_multi_interval_derived_from_first_strategy():
    multi = MultiStrategy([_AlwaysBuy(), _AlwaysBuy2()])
    assert multi.interval == "day"


def test_multi_params_prefixed_by_strategy_name():
    class _WithParams(Strategy):
        name = "p_strat"
        version = "v1"
        interval = "day"
        def __init__(self, rsi_max: float = 30.0):
            super().__init__(rsi_max=rsi_max)
        def on_bar(self, bar, portfolio): return []

    multi = MultiStrategy([_WithParams(rsi_max=25.0), _AlwaysBuy()])
    assert "p_strat.rsi_max" in multi.params
    assert multi.params["p_strat.rsi_max"] == 25.0


def test_multi_raises_on_empty_strategies():
    with pytest.raises(ValueError, match="at least one"):
        MultiStrategy([])


def test_multi_raises_on_mismatched_intervals():
    with pytest.raises(ValueError, match="interval"):
        MultiStrategy([_AlwaysBuy(), _IntradayStrat()])


def test_multi_raises_on_cross_sectional_sub_strategy():
    class _CS(Strategy):
        name = "cs"
        version = "v1"
        interval = "day"
        cross_sectional = True
        def on_bar(self, bar, portfolio): return []

    with pytest.raises(ValueError, match="cross-sectional"):
        MultiStrategy([_AlwaysBuy(), _CS()])


def test_multi_with_real_strategies_compatible_intervals():
    """momentum_v3 and swing_pullback share interval='day' — MultiStrategy accepts them."""
    from trading_system.strategy.momentum import MomentumV3
    from trading_system.strategy.swing_pullback import SwingPullback

    multi = MultiStrategy([MomentumV3(), SwingPullback()])
    assert multi.interval == "day"
    assert "momentum_v3.size_hint" in multi.params
    assert "swing_pullback.rsi_max" in multi.params


def test_multi_on_bar_with_real_strategy_features():
    """Smoke-test: swing_pullback entry fires through MultiStrategy when conditions met."""
    from trading_system.strategy.swing_pullback import SwingPullback

    class _Silent(Strategy):
        name = "silent"
        version = "v1"
        interval = "day"
        def on_bar(self, bar, portfolio): return []

    # Features that trigger swing_pullback entry (close>sma_200, close<=sma_20, rsi<=30)
    feats = {
        "sma_200": 90.0, "sma_50": 98.0, "sma_20": 101.0,
        "rsi_14": 25.0, "atr_14": 2.0,
    }
    multi = MultiStrategy([SwingPullback(rsi_max=30.0), _Silent()])
    sigs = multi.on_bar(make_bar(1, close=100.0, features=feats), _FakePortfolio())
    assert len(sigs) == 1
    assert sigs[0].side == Side.BUY
    assert sigs[0].symbol == "TEST"
