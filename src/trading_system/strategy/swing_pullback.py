"""Swing pullback-to-trend (daily bars, multi-day hold, long-only).

Thesis: in an established uptrend, buy temporary dips to the short moving average and hold for
a 1–2 week swing.

Entry (long): uptrend (`close > sma_200`) AND a pullback (`close <= sma_20`) AND a *deep enough*
dip (`rsi_14 <= rsi_max`). Exit: trend break (`close < sma_50`) or a max holding period
(`bars_held >= max_hold`), with a protective k×ATR trailing stop carried by the engine.

`rsi_max` is the ONLY lever that matters (a sweep showed `atr_stop_mult`/`max_hold` make no
difference — trades almost always exit on the `close < sma_50` trend-break before a stop or the
hold cap fires). It controls selectivity and therefore turnover. A loose value (≈45) buys every
shallow dip — high turnover whose per-trade edge can't clear CNC's 0.2% round-trip STT
(net-negative). The edge is concentrated in DEEP dips, so the default 30 takes only those:
full-period Sharpe ≈1.0, net-positive, ~1% max DD, and 67% of rolling windows profitable — but
only ~60 trades over 2yr×50 names (thin; that is the cost of conviction). Loosen to ≈35 for ~4×
more trades at a lower Sharpe (≈0.34). Validate any change on `backtest-rolling` before trusting
it. NOTE: the edge is risk-adjusted, not bull-beating — alpha vs buy-and-hold is negative in a
strong up-market; pair with a trend/momentum strategy for an all-weather book.

Uses the existing daily feature set v2 (sma_200/sma_50/sma_20, atr_14, rsi_14). Overnight (CNC).
"""
from trading_system.replay.events import BarEvent
from trading_system.strategy.base import PortfolioView, Strategy
from trading_system.strategy.signals import Side, Signal


class SwingPullback(Strategy):
    name = "swing_pullback"
    version = "v1"
    interval = "day"

    def __init__(
        self,
        size_hint: float = 0.10,
        rsi_max: float = 30.0,   # deep dips only — where the edge concentrates (sweep-validated)
        atr_stop_mult: float = 3.0,   # protective trailing stop (k×ATR)
        max_hold: float = 10,         # ~2 trading weeks
    ) -> None:
        super().__init__(
            size_hint=size_hint, rsi_max=rsi_max, atr_stop_mult=atr_stop_mult, max_hold=max_hold,
        )
        self._size_hint = size_hint
        self._rsi_max = rsi_max
        self._atr_stop_mult = atr_stop_mult
        self._max_hold = int(max_hold)

    def on_bar(self, bar: BarEvent, portfolio: PortfolioView) -> list[Signal]:
        f = bar.features
        sma_200 = f.get("sma_200")
        sma_50 = f.get("sma_50")
        sma_20 = f.get("sma_20")
        rsi = f.get("rsi_14")

        if portfolio.has_position(bar.symbol):
            if sma_50 is not None and bar.close < sma_50:
                return [Signal(symbol=bar.symbol, side=Side.SELL, reason="trend_break close<sma50")]
            if portfolio.bars_held(bar.symbol) >= self._max_hold:
                return [Signal(symbol=bar.symbol, side=Side.SELL, reason="max_hold")]
            return []

        if None in (sma_200, sma_50, sma_20, rsi):
            return []

        if bar.close > sma_200 and bar.close <= sma_20 and rsi <= self._rsi_max:
            return [Signal(
                symbol=bar.symbol, side=Side.BUY,
                reason=f"pullback_uptrend sma200={sma_200:.2f} rsi={rsi:.1f}",
                size_hint=self._size_hint,
                trailing_atr_mult=self._atr_stop_mult,
            )]
        return []
