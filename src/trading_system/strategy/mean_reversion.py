"""Mean-Reversion reference strategy (long-only, daily bars).

Thesis: short-term overreactions snap back — buy oversold dips, sell at the mean.

Entry (long): rsi_14 < 30 (oversold) AND close < sma_20 (below mean → room to revert).
Exit:         rsi_14 > 50 (reverted), OR close >= sma_20 (mean reached), OR
              stop-loss (engine, intrabar), OR time-stop after max_hold_bars.

Reads precomputed features from bar.features: rsi_14, sma_20.
"""
from trading_system.replay.events import BarEvent
from trading_system.strategy.base import PortfolioView, Strategy
from trading_system.strategy.signals import Side, Signal


class MeanReversionStrategy(Strategy):
    name = "mean_reversion"
    version = "v1"
    interval = "day"

    def __init__(
        self,
        size_hint: float = 0.10,    # target 10% of equity per position
        stop_pct: float = 0.05,     # tighter 5% stop
        rsi_entry: float = 30.0,
        rsi_exit: float = 50.0,
        max_hold_bars: int = 10,    # time-stop
    ) -> None:
        super().__init__(
            size_hint=size_hint, stop_pct=stop_pct,
            rsi_entry=rsi_entry, rsi_exit=rsi_exit, max_hold_bars=max_hold_bars,
        )
        self._size_hint = size_hint
        self._stop_pct = stop_pct
        self._rsi_entry = rsi_entry
        self._rsi_exit = rsi_exit
        self._max_hold_bars = max_hold_bars

    def on_bar(self, bar: BarEvent, portfolio: PortfolioView) -> list[Signal]:
        f = bar.features
        rsi = f.get("rsi_14")
        sma_20 = f.get("sma_20")

        if rsi is None or sma_20 is None:
            return []

        signals: list[Signal] = []
        holding = portfolio.has_position(bar.symbol)

        if not holding:
            if rsi < self._rsi_entry and bar.close < sma_20:
                signals.append(Signal(
                    symbol=bar.symbol,
                    side=Side.BUY,
                    reason=f"oversold rsi={rsi:.1f} close<sma20",
                    size_hint=self._size_hint,
                    stop_pct=self._stop_pct,
                ))
        else:
            reverted = rsi > self._rsi_exit
            reached_mean = bar.close >= sma_20
            timed_out = portfolio.bars_held(bar.symbol) >= self._max_hold_bars
            if reverted or reached_mean or timed_out:
                if timed_out and not (reverted or reached_mean):
                    reason = f"time_stop bars>={self._max_hold_bars}"
                elif reached_mean:
                    reason = "mean_reached close>=sma20"
                else:
                    reason = f"reverted rsi={rsi:.1f}"
                signals.append(Signal(symbol=bar.symbol, side=Side.SELL, reason=reason))

        return signals


class MeanReversionV2(MeanReversionStrategy):
    """Mean-reversion v2: cost-aware — trades less, demands deeper oversold.

    Two changes over v1, both aimed at the diagnosed killer (overtrading costs):
    - Stricter entry: rsi_entry=20 (deeper oversold) — fewer, higher-conviction trades.
    - Minimum hold: don't take the quick reverted/mean-reached exit before min_hold_bars,
      so we don't churn in and out paying fees on tiny round-trips. Stop-loss and the
      time-stop still apply unconditionally.

    Uses the v1 feature set (rsi_14, sma_20) — no v2 features required. Keeps the
    no-averaging-down guard (only enters when flat).
    """
    name = "mean_reversion_v2"
    version = "v2"
    interval = "day"

    def __init__(
        self,
        size_hint: float = 0.10,
        stop_pct: float = 0.05,
        rsi_entry: float = 20.0,    # deeper oversold than v1's 30
        rsi_exit: float = 50.0,
        max_hold_bars: int = 10,
        min_hold_bars: int = 3,     # block quick exits to cut churn/costs
    ) -> None:
        super().__init__(
            size_hint=size_hint, stop_pct=stop_pct,
            rsi_entry=rsi_entry, rsi_exit=rsi_exit, max_hold_bars=max_hold_bars,
        )
        self.params["min_hold_bars"] = min_hold_bars
        self._min_hold_bars = min_hold_bars

    def on_bar(self, bar: BarEvent, portfolio: PortfolioView) -> list[Signal]:
        f = bar.features
        rsi = f.get("rsi_14")
        sma_20 = f.get("sma_20")
        if rsi is None or sma_20 is None:
            return []

        holding = portfolio.has_position(bar.symbol)
        if not holding:
            if rsi < self._rsi_entry and bar.close < sma_20:
                return [Signal(
                    symbol=bar.symbol, side=Side.BUY,
                    reason=f"oversold rsi={rsi:.1f} close<sma20",
                    size_hint=self._size_hint, stop_pct=self._stop_pct,
                )]
            return []

        bars_held = portfolio.bars_held(bar.symbol)
        reverted = rsi > self._rsi_exit
        reached_mean = bar.close >= sma_20
        timed_out = bars_held >= self._max_hold_bars

        # Honour the minimum hold for the "soft" exits; time-stop always fires.
        if timed_out:
            return [Signal(symbol=bar.symbol, side=Side.SELL, reason=f"time_stop bars>={self._max_hold_bars}")]
        if bars_held >= self._min_hold_bars and (reverted or reached_mean):
            reason = "mean_reached close>=sma20" if reached_mean else f"reverted rsi={rsi:.1f}"
            return [Signal(symbol=bar.symbol, side=Side.SELL, reason=reason)]
        return []
