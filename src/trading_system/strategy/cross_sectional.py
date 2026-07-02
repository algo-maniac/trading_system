"""Cross-sectional momentum (long-only top-N).

Thesis: relative strength persists — the stocks that led the universe over the past year
keep leading. Rather than timing entries on each name, we rank the whole universe every
rebalance and simply hold the top-N highest-momentum names, equal-weight, rest in cash.

Ranking signal: momentum_252_21 (12-month return ending one month ago) from feature set v3.
This is a CROSS-SECTIONAL strategy — it needs every symbol at a timestamp at once, so it
overrides on_bars() and sets cross_sectional = True.

Rebalance mechanics (deterministic, lookahead-free, respects next-bar-open fills):
  - Every `rebalance_bars` timestamps, rank the present symbols and pick the top-N target.
  - SELL held names that fell out of the target (fills next bar → frees cash).
  - Queue the new entries; emit their BUYs on the FOLLOWING timestamp, once the sells have
    filled and cash has settled. This sell-then-buy sequencing lets a fully-invested book
    rotate names without the risk layer's cash / max_positions caps rejecting replacements.

Regime filter (`breadth_min` > 0 enables it): at each rebalance, breadth = fraction of the
present universe trading above its 200-day SMA. Below the threshold the target book is
empty — everything is sold and no new entries queue until breadth recovers at a later
rebalance. Momentum crashes concentrate in broad downtrends; sitting in cash there is the
standard drawdown control for cross-sectional momentum.

Crash quarantine (demerger guard): a single-bar close-to-close drop beyond `crash_threshold`
(default −30%) is almost never a real shareholder loss on an NSE large/mid cap — it is an
UNADJUSTED corporate action (demerger/special situation) in the data (ADANIENT 2015-06,
CGPOWER 2016-03), and it poisons the name's momentum_252_21 for a full year. Such names are
excluded from ranking for `crash_quarantine_bars` (≈252) and force-sold if held. Real crashes
(YESBANK-2020-style) are also caught — being out of a −56%-in-a-day name for a year is the
right side of that trade anyway.
"""
from trading_system.observability import get_logger
from trading_system.replay.events import BarEvent
from trading_system.strategy.base import PortfolioView, Strategy
from trading_system.strategy.signals import Side, Signal

logger = get_logger(__name__)

_MOMENTUM_FEATURE = "momentum_252_21"


class CrossSectionalMomentum(Strategy):
    name = "xsec_momentum"
    version = "v1"
    interval = "day"
    cross_sectional = True

    def __init__(
        self,
        top_n: float = 10,
        rebalance_bars: float = 21,
        breadth_min: float = 0.0,
        abs_momentum_min: float = -1e9,
        trailing_stop_pct: float = 0.0,
        crash_quarantine_bars: float = 252,
        crash_threshold: float = -0.30,
    ) -> None:
        # Params arrive as numbers from the CLI --param passthrough; coerce to int.
        super().__init__(
            top_n=top_n,
            rebalance_bars=rebalance_bars,
            breadth_min=breadth_min,
            abs_momentum_min=abs_momentum_min,
            trailing_stop_pct=trailing_stop_pct,
            crash_quarantine_bars=crash_quarantine_bars,
            crash_threshold=crash_threshold,
        )
        self.top_n = int(top_n)
        self.rebalance_bars = max(1, int(rebalance_bars))
        self.breadth_min = float(breadth_min)
        # Dual momentum: a name must clear this absolute 12-1 return to be holdable at
        # all (default effectively -inf = disabled). Unlike the binary breadth filter,
        # this shrinks the book name-by-name as the market rolls over.
        self.abs_momentum_min = float(abs_momentum_min)
        # Per-name engine-side trailing stop (fraction below running peak; 0 = disabled).
        # Reacts to individual breakdowns within days instead of waiting for the next
        # rebalance — the crash-tail control the rebalance-cadence filters can't provide.
        self.trailing_stop_pct = float(trailing_stop_pct)
        # Crash quarantine (see module docstring). 0 bars disables the guard.
        self.crash_quarantine_bars = max(0, int(crash_quarantine_bars))
        self.crash_threshold = float(crash_threshold)
        self._size_hint = 1.0 / self.top_n if self.top_n > 0 else 0.0
        self._bar_count = 0
        self._pending_buys: list[str] = []  # new entries awaiting cash settlement
        self._prev_close: dict[str, float] = {}
        self._quarantined_until: dict[str, int] = {}  # symbol -> bar_count when re-admitted

    def on_bar(self, bar: BarEvent, portfolio: PortfolioView) -> list[Signal]:
        # Unused: this strategy is driven via on_bars(). Defined to satisfy the ABC.
        return []

    @staticmethod
    def _breadth(bars: list[BarEvent]) -> float:
        """Fraction of the present universe above its 200-day SMA (1.0 during warmup)."""
        readable = [b for b in bars if b.features.get("sma_200") is not None]
        if not readable:
            return 1.0
        above = sum(1 for b in readable if b.close > b.features["sma_200"])
        return above / len(readable)

    def _update_quarantine(self, bars: list[BarEvent]) -> None:
        """Detect crash-sized close-to-close drops and quarantine those names."""
        if self.crash_quarantine_bars <= 0:
            for b in bars:
                self._prev_close[b.symbol] = b.close
            return
        for b in bars:
            prev = self._prev_close.get(b.symbol)
            if prev and prev > 0 and (b.close / prev - 1.0) < self.crash_threshold:
                self._quarantined_until[b.symbol] = self._bar_count + self.crash_quarantine_bars
                logger.warning(
                    "xsec_crash_quarantine", symbol=b.symbol,
                    drop=round(b.close / prev - 1.0, 4),
                    until_bar=self._quarantined_until[b.symbol],
                )
            self._prev_close[b.symbol] = b.close

    def _is_quarantined(self, symbol: str) -> bool:
        until = self._quarantined_until.get(symbol)
        if until is None:
            return False
        if self._bar_count >= until:
            del self._quarantined_until[symbol]
            return False
        return True

    def on_bars(self, bars: list[BarEvent], portfolio: PortfolioView) -> list[Signal]:
        signals: list[Signal] = []
        present = {b.symbol for b in bars}
        self._update_quarantine(bars)

        # Force-exit any held name that just got quarantined (its price series — and
        # therefore its momentum rank — can no longer be trusted).
        held_now = set(portfolio.open_symbols())
        for sym in sorted(held_now):
            if self._is_quarantined(sym):
                signals.append(Signal(
                    symbol=sym, side=Side.SELL, reason="xsec_quarantine crash_bar",
                ))

        # 1. Drain buys queued at the previous rebalance — sells have now filled, cash freed.
        if self._pending_buys:
            still_pending: list[str] = []
            for sym in self._pending_buys:
                if sym not in present:
                    still_pending.append(sym)  # no bar to fill against yet; retry next ts
                elif not portfolio.has_position(sym):
                    signals.append(Signal(
                        symbol=sym, side=Side.BUY,
                        reason=f"xsec_entry top{self.top_n}",
                        size_hint=self._size_hint,
                        trailing_stop_pct=self.trailing_stop_pct or None,
                    ))
            self._pending_buys = still_pending

        # 2. Rebalance on the cadence boundary.
        is_rebalance = self._bar_count % self.rebalance_bars == 0
        self._bar_count += 1
        if not is_rebalance:
            return signals

        candidates = [
            b for b in bars
            if b.features.get(_MOMENTUM_FEATURE) is not None
            and b.features[_MOMENTUM_FEATURE] > self.abs_momentum_min
            and not self._is_quarantined(b.symbol)
        ]
        # Deterministic rank: highest momentum first, symbol as tiebreak.
        candidates.sort(key=lambda b: (-b.features[_MOMENTUM_FEATURE], b.symbol))
        target = [b.symbol for b in candidates[: self.top_n]]
        if self.breadth_min > 0 and self._breadth(bars) < self.breadth_min:
            target = []  # risk-off: empty book until breadth recovers
            self._pending_buys.clear()
        target_set = set(target)
        held = set(portfolio.open_symbols())

        # SELL held names that dropped out of the target.
        for sym in sorted(held):
            if sym not in target_set:
                signals.append(Signal(
                    symbol=sym, side=Side.SELL, reason="xsec_drop dropped_from_top_n",
                ))

        # Queue new entries (target minus held) for the next timestamp's drain.
        for sym in target:
            if sym not in held and sym not in self._pending_buys:
                self._pending_buys.append(sym)

        return signals
