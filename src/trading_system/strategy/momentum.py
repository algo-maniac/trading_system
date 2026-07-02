"""Momentum / Trend-Following reference strategy (long-only, daily bars).

Thesis: trends persist — buy established uptrends, exit when the trend breaks.

Entry (long): sma_20 crosses ABOVE sma_50 (fresh golden cross) AND momentum_20 > 0.
Exit:         sma_20 crosses BELOW sma_50 (death cross), OR stop-loss (engine, intrabar).

Reads precomputed features from bar.features: sma_20, sma_50, momentum_20.
Holds minimal per-symbol state (prior sma relationship) to detect a *fresh* cross —
updated only from past bars, so the strategy stays deterministic and lookahead-free.
"""
from trading_system.replay.events import BarEvent
from trading_system.strategy.base import PortfolioView, Strategy
from trading_system.strategy.signals import Side, Signal


class MomentumStrategy(Strategy):
    name = "momentum"
    version = "v1"
    interval = "day"

    def __init__(
        self,
        size_hint: float = 0.10,   # target 10% of equity per position
        stop_pct: float = 0.08,    # 8% stop below entry
    ) -> None:
        super().__init__(size_hint=size_hint, stop_pct=stop_pct)
        self._size_hint = size_hint
        self._stop_pct = stop_pct
        self._prev_above: dict[str, bool] = {}  # symbol -> was sma_20 > sma_50 last bar

    def on_bar(self, bar: BarEvent, portfolio: PortfolioView) -> list[Signal]:
        f = bar.features
        sma_20 = f.get("sma_20")
        sma_50 = f.get("sma_50")
        momentum_20 = f.get("momentum_20")

        # Warmup: indicators not yet available
        if sma_20 is None or sma_50 is None or momentum_20 is None:
            return []

        now_above = sma_20 > sma_50
        prev_above = self._prev_above.get(bar.symbol)
        self._prev_above[bar.symbol] = now_above  # update state for next bar

        signals: list[Signal] = []
        holding = portfolio.has_position(bar.symbol)

        if not holding:
            # Fresh golden cross: was not above (or unknown) last bar, is above now
            fresh_cross = prev_above is False and now_above
            if fresh_cross and momentum_20 > 0:
                signals.append(Signal(
                    symbol=bar.symbol,
                    side=Side.BUY,
                    reason=f"golden_cross sma20>{sma_50:.2f} mom20={momentum_20:.4f}",
                    size_hint=self._size_hint,
                    stop_pct=self._stop_pct,
                ))
        else:
            # Death cross → exit
            if not now_above:
                signals.append(Signal(
                    symbol=bar.symbol,
                    side=Side.SELL,
                    reason="death_cross sma20<sma50",
                ))

        return signals


class MomentumV2(MomentumStrategy):
    """Momentum v2: adds a long-term regime filter and a trailing stop.

    Two changes over v1, each targeting a diagnosed weakness:
    - Regime filter: only enter when close > sma_200 (trade with the primary trend) —
      cuts the whipsaw false-breakouts that bled v1 in choppy 2024/2025.
    - Trailing stop (off the running peak) replaces the fixed stop — lets winners run
      further, which is what trend-following needs to fix its win/loss math.

    Requires the v2 feature set (sma_200). Run with --feature-version v2.
    """
    name = "momentum_v2"
    version = "v2"
    interval = "day"

    def __init__(
        self,
        size_hint: float = 0.10,
        trailing_stop_pct: float = 0.12,   # wider trailing stop — give trends room
    ) -> None:
        # Skip MomentumStrategy.__init__ (its fixed stop_pct doesn't apply here).
        Strategy.__init__(self, size_hint=size_hint, trailing_stop_pct=trailing_stop_pct)
        self._size_hint = size_hint
        self._trailing_stop_pct = trailing_stop_pct
        self._stop_pct = 0.0  # unused; exits via trailing stop or death cross
        self._prev_above: dict[str, bool] = {}

    def on_bar(self, bar: BarEvent, portfolio: PortfolioView) -> list[Signal]:
        f = bar.features
        sma_20 = f.get("sma_20")
        sma_50 = f.get("sma_50")
        momentum_20 = f.get("momentum_20")
        sma_200 = f.get("sma_200")

        if sma_20 is None or sma_50 is None or momentum_20 is None or sma_200 is None:
            return []

        now_above = sma_20 > sma_50
        prev_above = self._prev_above.get(bar.symbol)
        self._prev_above[bar.symbol] = now_above

        signals: list[Signal] = []
        holding = portfolio.has_position(bar.symbol)

        if not holding:
            fresh_cross = prev_above is False and now_above
            regime_ok = bar.close > sma_200
            if fresh_cross and momentum_20 > 0 and regime_ok:
                signals.append(Signal(
                    symbol=bar.symbol,
                    side=Side.BUY,
                    reason=f"golden_cross+regime sma200={sma_200:.2f} mom20={momentum_20:.4f}",
                    size_hint=self._size_hint,
                    trailing_stop_pct=self._trailing_stop_pct,
                ))
        else:
            if not now_above:
                signals.append(Signal(
                    symbol=bar.symbol,
                    side=Side.SELL,
                    reason="death_cross sma20<sma50",
                ))

        return signals


class MomentumV3(MomentumV2):
    """Momentum v3: regime filter + ATR-based trailing stop.

    Same golden-cross + sma_200 regime entry as v2, but replaces the flat
    trailing_stop_pct with a k×atr_14 trailing stop.  Because ATR is in price
    units, the stop width automatically adapts to each stock's volatility —
    wider for volatile names, tighter for calm ones — directly fixing the
    whipsaw problem diagnosed in v2.

    Requires the v2 feature set (sma_200, atr_14). Run with --feature-version v2.
    If atr_14 is absent on the entry bar the stop seeds lazily once ATR appears
    on a subsequent bar; no crash, just a wider-than-intended initial exposure.
    """
    name = "momentum_v3"
    version = "v3"
    interval = "day"

    def __init__(
        self,
        size_hint: float = 0.10,
        trailing_atr_mult: float = 3.0,
    ) -> None:
        Strategy.__init__(self, size_hint=size_hint, trailing_atr_mult=trailing_atr_mult)
        self._size_hint = size_hint
        self._trailing_atr_mult = trailing_atr_mult
        self._stop_pct = 0.0
        self._trailing_stop_pct = 0.0  # unused — exits via ATR stop or death cross
        self._prev_above: dict[str, bool] = {}

    def on_bar(self, bar: BarEvent, portfolio: PortfolioView) -> list[Signal]:
        f = bar.features
        sma_20 = f.get("sma_20")
        sma_50 = f.get("sma_50")
        momentum_20 = f.get("momentum_20")
        sma_200 = f.get("sma_200")

        if sma_20 is None or sma_50 is None or momentum_20 is None or sma_200 is None:
            return []

        now_above = sma_20 > sma_50
        prev_above = self._prev_above.get(bar.symbol)
        self._prev_above[bar.symbol] = now_above

        signals: list[Signal] = []
        holding = portfolio.has_position(bar.symbol)

        if not holding:
            fresh_cross = prev_above is False and now_above
            regime_ok = bar.close > sma_200
            if fresh_cross and momentum_20 > 0 and regime_ok:
                signals.append(Signal(
                    symbol=bar.symbol,
                    side=Side.BUY,
                    reason=f"golden_cross+regime+atr_stop sma200={sma_200:.2f} mom20={momentum_20:.4f}",
                    size_hint=self._size_hint,
                    trailing_atr_mult=self._trailing_atr_mult,
                ))
        else:
            if not now_above:
                signals.append(Signal(
                    symbol=bar.symbol,
                    side=Side.SELL,
                    reason="death_cross sma20<sma50",
                ))

        return signals
