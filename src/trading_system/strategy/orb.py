"""Opening-Range Breakout (intraday, long-only).

Thesis: on trending days, a decisive break above the opening range (first ~15 min high) with
volume tends to continue.

Entry (long): `close > or_high` AND volume expansion (`rel_volume >= vol_mult`) AND a trend
regime (`adx_14 >= adx_min`), once the opening range has formed (`bar_in_session >= or_bars`).
The engine blocks late-session entries and squares off before the close (intraday=True).

Exit: fixed stop at ~one opening-range-width below entry and a take-profit at `target_r ×
or_width` above entry (both via the engine's intrabar stop/target), else EOD square-off.
Requires feature set `intraday_v1`.
"""
from trading_system.replay.events import BarEvent
from trading_system.strategy.base import PortfolioView, Strategy
from trading_system.strategy.signals import Side, Signal


class OpeningRangeBreakout(Strategy):
    name = "orb"
    version = "v1"
    interval = "5minute"
    intraday = True

    def __init__(
        self,
        size_hint: float = 0.20,
        vol_mult: float = 1.5,    # require volume >= vol_mult × the session's average-so-far
        adx_min: float = 20.0,    # only on trend days
        or_bars: float = 3,       # opening range must have formed
        target_r: float = 2.0,    # take-profit at target_r × opening-range width
        gap_min: float = 0.0,     # only trade gap-up event days: gap_pct >= gap_min (0 = off;
                                  # needs feature set intraday_v2 which carries gap_pct)
    ) -> None:
        super().__init__(
            size_hint=size_hint, vol_mult=vol_mult, adx_min=adx_min,
            or_bars=or_bars, target_r=target_r, gap_min=gap_min,
        )
        self._size_hint = size_hint
        self._vol_mult = vol_mult
        self._adx_min = adx_min
        self._or_bars = int(or_bars)
        self._target_r = target_r
        self._gap_min = gap_min

    def on_bar(self, bar: BarEvent, portfolio: PortfolioView) -> list[Signal]:
        if portfolio.has_position(bar.symbol):
            return []  # exits handled by the engine (stop / target / EOD square-off)

        f = bar.features
        or_high = f.get("or_high")
        or_width = f.get("or_width")
        rel_volume = f.get("rel_volume")
        adx = f.get("adx_14")
        bar_in_session = f.get("bar_in_session")
        if None in (or_high, or_width, rel_volume, adx, bar_in_session):
            return []
        if bar_in_session < self._or_bars or or_width <= 0 or bar.close <= 0:
            return []

        # Event-day filter: require a gap-up of at least gap_min (long-only, so gap-down
        # days are skipped too). gap_pct is None on the frame's first session or when the
        # feature set doesn't carry it — treat as "no event", skip when the filter is on.
        if self._gap_min > 0:
            gap = f.get("gap_pct")
            if gap is None or gap < self._gap_min:
                return []

        if bar.close > or_high and rel_volume >= self._vol_mult and adx >= self._adx_min:
            # Stop ~one OR-width below entry; target target_r OR-widths above (as entry fractions).
            stop_pct = or_width / bar.close
            target_pct = (self._target_r * or_width) / bar.close
            return [Signal(
                symbol=bar.symbol, side=Side.BUY,
                reason=f"orb_breakout relvol={rel_volume:.2f} adx={adx:.1f} or_w={or_width:.2f}",
                size_hint=self._size_hint,
                stop_pct=stop_pct,
                target_pct=target_pct,
            )]
        return []
