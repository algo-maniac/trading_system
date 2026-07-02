"""VWAP mean-reversion (intraday, long-only).

Thesis: on range-bound days price oscillates around the session VWAP; fade stretched moves
*below* VWAP and ride the snap back toward it.

Entry (long): price stretched below VWAP (`vwap_z <= -z_entry`) AND the stretch is large enough
in *absolute* terms to clear round-trip costs with margin (`vwap_dist <= -min_edge_pct`) AND a
range regime (`adx_14 <= adx_max`) AND oversold (`rsi_14 <= rsi_max`), only after the opening
range has formed (`bar_in_session >= or_bars`). The engine blocks entries late in the session
and squares off before the close (intraday=True), so no time-of-day check is needed here.

The `min_edge_pct` gate is what makes this tradable net of cost: the reversion target is the
trip back to VWAP, i.e. ~`|vwap_dist|`. A z-score trigger alone fires on tiny absolute moves
whose target can't pay the ~0.2% round-trip MIS cost — those are guaranteed cost-losers. We only
take stretches whose own target clears cost by a comfortable margin.

Exit: snap back to VWAP (`close >= vwap`) or a time-stop (`bars_held >= time_stop_bars`); a
*fixed* k×ATR protective stop (seeded from entry-bar ATR, NOT trailing) gives the reversion room
to play out — a trailing stop would ratchet up and get tripped on the first pullback before
price reaches VWAP, manufacturing losses out of would-be wins. Requires feature set `intraday_v1`.
"""
from trading_system.replay.events import BarEvent
from trading_system.strategy.base import PortfolioView, Strategy
from trading_system.strategy.signals import Side, Signal


class VWAPReversion(Strategy):
    name = "vwap_reversion"
    version = "v1"
    interval = "5minute"
    intraday = True

    def __init__(
        self,
        size_hint: float = 0.20,
        z_entry: float = 1.5,        # enter when vwap_z <= -z_entry (stretched below VWAP)
        min_edge_pct: float = 0.004, # AND ≥0.4% below VWAP so the target clears round-trip cost
        adx_max: float = 25.0,       # only on range days (low trend strength)
        rsi_max: float = 35.0,       # oversold confirmation
        or_bars: float = 3,          # wait for the opening range to form
        time_stop_bars: float = 12,  # bail if the snap-back hasn't happened
        atr_stop_mult: float = 2.0,  # fixed protective stop (k×ATR below entry, non-trailing)
    ) -> None:
        super().__init__(
            size_hint=size_hint, z_entry=z_entry, min_edge_pct=min_edge_pct,
            adx_max=adx_max, rsi_max=rsi_max, or_bars=or_bars,
            time_stop_bars=time_stop_bars, atr_stop_mult=atr_stop_mult,
        )
        self._size_hint = size_hint
        self._z_entry = z_entry
        self._min_edge_pct = min_edge_pct
        self._adx_max = adx_max
        self._rsi_max = rsi_max
        self._or_bars = int(or_bars)
        self._time_stop_bars = int(time_stop_bars)
        self._atr_stop_mult = atr_stop_mult

    def on_bar(self, bar: BarEvent, portfolio: PortfolioView) -> list[Signal]:
        f = bar.features
        vwap = f.get("vwap")

        if portfolio.has_position(bar.symbol):
            # Primary exit: reverted to/above VWAP (target reached).
            if vwap is not None and bar.close >= vwap:
                return [Signal(symbol=bar.symbol, side=Side.SELL, reason="vwap_revert")]
            # Time-stop: the mean-reversion thesis didn't play out fast enough.
            if portfolio.bars_held(bar.symbol) >= self._time_stop_bars:
                return [Signal(symbol=bar.symbol, side=Side.SELL, reason="time_stop")]
            return []

        vwap_z = f.get("vwap_z")
        vwap_dist = f.get("vwap_dist")
        adx = f.get("adx_14")
        rsi = f.get("rsi_14")
        atr = f.get("atr_14")
        bar_in_session = f.get("bar_in_session")
        if None in (vwap_z, vwap_dist, adx, rsi, bar_in_session):
            return []
        if bar_in_session < self._or_bars:
            return []

        # Stretched below VWAP in BOTH scale-free (z) and absolute (cost-clearing) terms.
        stretched = vwap_z <= -self._z_entry and vwap_dist <= -self._min_edge_pct
        if stretched and adx <= self._adx_max and rsi <= self._rsi_max:
            # Fixed k×ATR protective stop (fraction of entry price); engine seeds it once at
            # the fill and never ratchets it — the reversion gets room to reach VWAP.
            stop_pct = (self._atr_stop_mult * atr / bar.close) if atr else None
            return [Signal(
                symbol=bar.symbol, side=Side.BUY,
                reason=f"vwap_revert_entry z={vwap_z:.2f} dist={vwap_dist * 100:.2f}% adx={adx:.1f} rsi={rsi:.1f}",  # noqa: E501
                size_hint=self._size_hint,
                stop_pct=stop_pct,
            )]
        return []
