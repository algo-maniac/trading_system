"""Swing breakout (daily bars, multi-day hold, long-only).

Thesis: a close at a new N-day high in an uptrend kicks off a multi-day continuation.

Entry (long): `close >= highest high of the prior `lookback` days` (Donchian breakout) AND
uptrend (`close > sma_200`). Exit: max holding period (`bars_held >= max_hold`) with a
protective k×ATR trailing stop carried by the engine.

The N-day high is tracked internally from PAST bars (a per-symbol rolling deque), so no new
daily feature is needed — and it's lookahead-free (the current bar's high is excluded from the
prior-high it must break). Uses daily feature set v2 (sma_200, atr_14). Overnight (CNC).
"""
from collections import deque

from trading_system.replay.events import BarEvent
from trading_system.strategy.base import PortfolioView, Strategy
from trading_system.strategy.signals import Side, Signal


class SwingBreakout(Strategy):
    name = "swing_breakout"
    version = "v1"
    interval = "day"

    def __init__(
        self,
        size_hint: float = 0.10,
        lookback: float = 20,         # breakout above the prior `lookback`-day high
        atr_stop_mult: float = 3.0,
        max_hold: float = 10,
    ) -> None:
        super().__init__(
            size_hint=size_hint, lookback=lookback, atr_stop_mult=atr_stop_mult, max_hold=max_hold,
        )
        self._size_hint = size_hint
        self._lookback = int(lookback)
        self._atr_stop_mult = atr_stop_mult
        self._max_hold = int(max_hold)
        self._highs: dict[str, deque] = {}  # symbol -> last `lookback` PRIOR-bar highs

    def on_bar(self, bar: BarEvent, portfolio: PortfolioView) -> list[Signal]:
        sym = bar.symbol
        window = self._highs.setdefault(sym, deque(maxlen=self._lookback))
        # Highest high over the prior `lookback` bars (current bar excluded → no lookahead).
        prior_high = max(window) if len(window) == self._lookback else None
        window.append(bar.high)

        if portfolio.has_position(sym):
            if portfolio.bars_held(sym) >= self._max_hold:
                return [Signal(symbol=sym, side=Side.SELL, reason="max_hold")]
            return []

        sma_200 = bar.features.get("sma_200")
        if prior_high is None or sma_200 is None:
            return []

        if bar.close >= prior_high and bar.close > sma_200:
            return [Signal(
                symbol=sym, side=Side.BUY,
                reason=f"breakout_{self._lookback}d high={prior_high:.2f} sma200={sma_200:.2f}",
                size_hint=self._size_hint,
                trailing_atr_mult=self._atr_stop_mult,
            )]
        return []
