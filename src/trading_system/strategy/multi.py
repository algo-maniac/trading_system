"""Multi-strategy composite: fan out on_bar() to N per-symbol strategies, merge signals.

SELL takes precedence over BUY for the same symbol (conservative exit). Sub-strategies
share the portfolio view — each sees the combined book state before deciding, so:
  - A symbol already held by strategy A won't generate a new BUY from strategy B.
  - A SELL from any strategy closes the position regardless of which strategy opened it.

All sub-strategies must use the same interval and feature set — validated at construction.
Capital and risk limits apply to the combined book. Cross-sectional strategies are not
supported (use MultiStrategy only with per-symbol strategies).
"""
from trading_system.replay.events import BarEvent
from trading_system.strategy.base import PortfolioView, Strategy
from trading_system.strategy.signals import Side, Signal


class MultiStrategy(Strategy):
    """Wraps multiple per-symbol strategies; SELL beats BUY for the same symbol."""

    def __init__(self, strategies: list[Strategy]) -> None:
        if not strategies:
            raise ValueError("MultiStrategy requires at least one sub-strategy")
        intervals = {s.interval for s in strategies}
        if len(intervals) > 1:
            raise ValueError(
                f"All sub-strategies must share one interval, got: {intervals}"
            )
        if any(getattr(s, "cross_sectional", False) for s in strategies):
            raise ValueError("MultiStrategy does not support cross-sectional sub-strategies")
        super().__init__()
        self._strategies = strategies
        self.name = "+".join(s.name for s in strategies)
        self.version = "+".join(s.version for s in strategies)
        self.interval = strategies[0].interval
        # Params: each sub-strategy's params prefixed by its name for traceability.
        self.params = {
            f"{s.name}.{k}": v
            for s in strategies
            for k, v in s.params.items()
        }

    def on_bar(self, bar: BarEvent, portfolio: PortfolioView) -> list[Signal]:
        merged: dict[str, Signal] = {}
        for strat in self._strategies:
            for sig in strat.on_bar(bar, portfolio):
                if sig.symbol not in merged or sig.side == Side.SELL:
                    merged[sig.symbol] = sig
        return list(merged.values())
