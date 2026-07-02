"""BacktestEngine — orchestrates Strategy → Risk → Execution → Portfolio → Audit.

Consumes a generic Iterable[BarEvent] (production: ReplayEngine.run(); tests: a list),
so the core has no DB dependency.

Per-bar loop (bars arrive in (ts, symbol) order):
  1. age_position(symbol)         — increment holding period for an existing position
  2. fill pending order(s)        — at THIS bar's open (next-bar-open, no lookahead)
  3. check stops/targets intrabar — exit at stop/target (gap-aware)
  4. update last price            — for mark-to-market
  5. strategy → risk → queue      — new orders queued for the symbol's NEXT bar
On each timestamp change: snapshot portfolio equity.

Deterministic: no RNG, no wall-clock; identical input stream → identical result.
"""
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from trading_system.analytics.metrics import compute_metrics
from trading_system.backtest.results import BacktestConfig, BacktestResult, SignalAudit
from trading_system.execution.fees import Product
from trading_system.execution.simulator import simulate_fill
from trading_system.observability import get_logger
from trading_system.portfolio.portfolio import Portfolio
from trading_system.replay.events import BarEvent
from trading_system.risk.manager import RiskManager
from trading_system.risk.models import RiskAction, RiskConfig
from trading_system.strategy.base import Strategy
from trading_system.strategy.signals import Side

logger = get_logger(__name__)


@dataclass
class _PendingOrder:
    side: Side
    qty: int
    stop_pct: float | None
    target_pct: float | None
    reason: str
    trailing_stop_pct: float | None = None
    trailing_atr_mult: float | None = None


class BacktestEngine:
    def __init__(
        self,
        strategy: Strategy,
        config: BacktestConfig,
        risk_config: RiskConfig | None = None,
        product: Product = Product.CNC,
    ) -> None:
        self.strategy = strategy
        self.config = config
        self.portfolio = Portfolio(config.initial_capital)
        self.risk = RiskManager(risk_config)
        self.product = product
        self.signals: list[SignalAudit] = []
        self._pending: dict[str, _PendingOrder] = {}
        # Risk-halt tracking. The halt's reference peak is the max equity over the last
        # `halt_peak_window_days` trading days (deque of one running peak per day) so the
        # halt RE-ARMS after a crash ages out of the window — an all-time peak freezes a
        # crashed book forever (see RiskConfig.halt_peak_window_days). Window 0 → all-time.
        self._daily_peaks: deque[float] = deque()
        self._day_start_equity = config.initial_capital
        self._current_date: date | None = None
        # Streaming cursor — shared by the batch loop and the incremental feed() path so a
        # forward/paper-test run is byte-identical to a batch run over the same bars.
        self._last_ts: datetime | None = None

    def run(self, bars: Iterable[BarEvent]) -> BacktestResult:
        # Cross-sectional strategies need every symbol at a timestamp at once → batched loop.
        # Per-symbol strategies stream one bar at a time through feed() (behaviour unchanged).
        if self.strategy.cross_sectional:
            self._run_batched(bars)
        else:
            for bar in bars:
                self.feed(bar)
        return self.finalize()

    def feed(self, bar: BarEvent) -> None:
        """Drive the engine one bar at a time — the per-symbol streaming step.

        Used by run() for batch backtests AND by the Backtester agent's forward/paper-test path
        to push live bars off the bus into a persistent engine. Snapshots equity when the
        timestamp advances, exactly like the original batch loop, so a streamed run produces an
        identical equity curve / trades / metrics to a batch run over the same bars.
        """
        if self._last_ts is not None and bar.ts != self._last_ts:
            self.portfolio.snapshot(self._last_ts)
        self._last_ts = bar.ts
        self._process_bar(bar)

    def finalize(self) -> BacktestResult:
        """Take the final equity snapshot and assemble the result. Call exactly once when the
        bar stream ends (batch) or at session close (forward-test) — snapshot() appends
        unconditionally, so repeated calls would duplicate the last equity point. For a live
        metrics read mid-session, read self.portfolio.equity() directly instead."""
        if self._last_ts is not None:
            self.portfolio.snapshot(self._last_ts)

        metrics = compute_metrics(
            self.portfolio.equity_curve, self.portfolio.trades,
            self.config.interval, self.config.initial_capital,
        )
        logger.info(
            "backtest_done",
            strategy=self.strategy.name,
            trades=len(self.portfolio.trades),
            final_equity=round(self.portfolio.equity(), 2),
            total_return=round(metrics.get("total_return", 0.0), 4),
        )
        return BacktestResult(
            config=self.config,
            equity_curve=self.portfolio.equity_curve,
            trades=self.portfolio.trades,
            signals=self.signals,
            metrics=metrics,
        )

    # -----------------------------------------------------------------------

    def _run_batched(self, bars: Iterable[BarEvent]) -> None:
        """Cross-sectional loop: buffer all bars sharing a timestamp, then process them
        as one group so the strategy can rank the full cross-section before any order.
        The trailing snapshot is taken by finalize() (using self._last_ts)."""
        buffer: list[BarEvent] = []
        for bar in bars:
            if self._last_ts is not None and bar.ts != self._last_ts:
                self._process_timestamp_batch(buffer)
                self.portfolio.snapshot(self._last_ts)
                buffer = []
            self._last_ts = bar.ts
            buffer.append(bar)
        if buffer:
            self._process_timestamp_batch(buffer)

    def _process_timestamp_batch(self, bars: list[BarEvent]) -> None:
        """Run per-symbol mechanics for every symbol at this timestamp (same step order as
        _process_bar 1–5), THEN make one coordinated strategy decision over all of them.

        Mechanics finish for all symbols before on_bars() is called, so orders queued at the
        previous timestamp have already filled (cash freed) before the strategy decides — this
        is what lets a fully-invested book rotate names via sell-then-buy sequencing.
        """
        for bar in bars:
            sym = bar.symbol
            self.portfolio.age_position(sym)
            pending = self._pending.pop(sym, None)
            if pending is not None:
                self._fill_pending(bar, pending)
            self._check_exits(bar)
            self.portfolio.update_price(sym, bar.close)
            self._ratchet_trailing_stop(bar)
        self._generate_orders_batch(bars)

    # -----------------------------------------------------------------------

    def _process_bar(self, bar: BarEvent) -> None:
        sym = bar.symbol

        # 1. Age an existing position (holding period counts completed bars)
        self.portfolio.age_position(sym)

        # 2. Fill any pending order at THIS bar's open (next-bar-open execution)
        pending = self._pending.pop(sym, None)
        if pending is not None:
            self._fill_pending(bar, pending)

        # 3. Stops / targets intrabar (gap-aware), if still holding
        self._check_exits(bar)

        # 4. Mark-to-market reference price
        self.portfolio.update_price(sym, bar.close)

        # 4b. Ratchet any trailing stop up off this bar's peak (affects NEXT bar's exit
        #     check only — never raises the stop and tests the low within the same bar,
        #     so no intrabar lookahead).
        self._ratchet_trailing_stop(bar)

        # 4c. Intraday session control: square off before the close + block late entries.
        #     Daily/overnight strategies (intraday=False) skip this entirely.
        if self.strategy.intraday and self._apply_session_policy(bar):
            return

        # 5. Strategy → Risk → queue for next bar
        self._generate_orders(bar)

    def _apply_session_policy(self, bar: BarEvent) -> bool:
        """Intraday-only session control. Returns True if NEW entries must be suppressed for
        this bar (caller then skips _generate_orders):

        - at/after `square_off_time`: queue a square-off SELL for any open position (no pending
          order yet) — it fills at the NEXT intra-session bar's open, so the book is flat before
          the close and nothing carries overnight — and suppress new entries;
        - at/after `no_entry_after`: suppress new entries (no square-off needed).
        """
        t = bar.ts.time()
        if t >= self.strategy.square_off_time:
            pos = self.portfolio.positions.get(bar.symbol)
            if pos is not None and bar.symbol not in self._pending:
                self._pending[bar.symbol] = _PendingOrder(
                    side=Side.SELL, qty=pos.qty, stop_pct=None, target_pct=None,
                    reason="eod_squareoff",
                )
            return True
        return t >= self.strategy.no_entry_after

    def _ratchet_trailing_stop(self, bar: BarEvent) -> None:
        pos = self.portfolio.positions.get(bar.symbol)
        if pos is None:
            return
        if pos.trailing_atr_mult is not None:
            # ATR-based path: stop width adapts to volatility. ATR path takes precedence.
            atr = bar.features.get("atr_14")
            if atr is not None:
                candidate = bar.high - pos.trailing_atr_mult * atr
                if pos.stop_price is None or candidate > pos.stop_price:
                    pos.stop_price = candidate
        elif pos.trailing_stop_pct is not None:
            candidate = bar.high * (1 - pos.trailing_stop_pct)
            if pos.stop_price is None or candidate > pos.stop_price:
                pos.stop_price = candidate

    def _fill_pending(self, bar: BarEvent, order: _PendingOrder) -> None:
        is_buy = order.side == Side.BUY
        if not is_buy and not self.portfolio.has_position(bar.symbol):
            return  # already exited (e.g. via stop) before this fill

        fill = simulate_fill(
            ts=bar.ts, symbol=bar.symbol, is_buy=is_buy, qty=order.qty,
            clean_price=bar.open, slippage_bps=self.config.slippage_bps,
            product=self.product, fees_enabled=self.config.fees_enabled,
        )
        trade = self.portfolio.apply_fill(fill, reason=order.reason)
        if is_buy:
            pos = self.portfolio.positions.get(bar.symbol)
            if pos is not None:
                if order.stop_pct is not None:
                    pos.stop_price = fill.price * (1 - order.stop_pct)
                if order.target_pct is not None:
                    pos.target_price = fill.price * (1 + order.target_pct)
                if order.trailing_stop_pct is not None:
                    # Trailing stop: seed at entry, then ratchet up off each bar's peak.
                    pos.trailing_stop_pct = order.trailing_stop_pct
                    initial = fill.price * (1 - order.trailing_stop_pct)
                    pos.stop_price = max(pos.stop_price, initial) if pos.stop_price is not None else initial
                if order.trailing_atr_mult is not None:
                    # ATR-based trailing stop: seed from entry-bar ATR if available.
                    # If ATR is missing, stop_price stays None until the ratchet sets it
                    # once ATR appears on a subsequent bar — no crash, no intrabar lookahead.
                    pos.trailing_atr_mult = order.trailing_atr_mult
                    atr = bar.features.get("atr_14")
                    if atr is not None:
                        initial = fill.price - order.trailing_atr_mult * atr
                        pos.stop_price = max(pos.stop_price, initial) if pos.stop_price is not None else initial
        if trade is not None:
            logger.debug("trade_closed", symbol=trade.symbol, pnl=round(trade.pnl, 2), reason=trade.exit_reason)

    def _check_exits(self, bar: BarEvent) -> None:
        pos = self.portfolio.positions.get(bar.symbol)
        if pos is None:
            return

        exit_price: float | None = None
        reason = ""

        # Stop first (conservative): if the low pierced the stop
        if pos.stop_price is not None and bar.low <= pos.stop_price:
            # Gap-down through the stop fills at the (worse) open
            exit_price = min(bar.open, pos.stop_price)
            reason = "stop_loss"
        elif pos.target_price is not None and bar.high >= pos.target_price:
            # Gap-up through the target fills at the (better) open
            exit_price = max(bar.open, pos.target_price)
            reason = "target"

        if exit_price is None:
            return

        fill = simulate_fill(
            ts=bar.ts, symbol=bar.symbol, is_buy=False, qty=pos.qty,
            clean_price=exit_price, slippage_bps=self.config.slippage_bps,
            product=self.product, fees_enabled=self.config.fees_enabled,
        )
        self.portfolio.apply_fill(fill, reason=reason)
        # A pending strategy-exit for this symbol is now moot
        self._pending.pop(bar.symbol, None)

    def _update_halt_state(self, eq: float, bar_date: date) -> tuple[float, float]:
        """Roll the day boundary + rolling-peak window; return (day_return_pct, drawdown).

        The drawdown here feeds the entry-halt only — it is measured against the max
        equity over the trailing `halt_peak_window_days` trading days so the halt can
        re-arm after a crash leaves the window. Reporting metrics (Portfolio.snapshot /
        max_drawdown) remain all-time and are untouched by this.
        """
        window = self.risk.config.halt_peak_window_days
        if bar_date != self._current_date:
            self._current_date = bar_date
            self._day_start_equity = eq
            self._daily_peaks.append(eq)
            while window > 0 and len(self._daily_peaks) > window:
                self._daily_peaks.popleft()
        else:
            self._daily_peaks[-1] = max(self._daily_peaks[-1], eq)
        peak = max(self._daily_peaks)
        day_return_pct = (eq - self._day_start_equity) / self._day_start_equity if self._day_start_equity else 0.0
        current_drawdown = (eq - peak) / peak if peak > 0 else 0.0
        return day_return_pct, current_drawdown

    def _generate_orders(self, bar: BarEvent) -> None:
        eq = self.portfolio.equity()
        day_return_pct, current_drawdown = self._update_halt_state(eq, bar.ts.date())

        for signal in self.strategy.on_bar(bar, self.portfolio):
            decision = self.risk.evaluate(
                signal, self.portfolio, ref_price=bar.close,
                day_return_pct=day_return_pct, current_drawdown=current_drawdown,
            )
            self.signals.append(SignalAudit(
                ts=bar.ts, symbol=signal.symbol, side=signal.side.value,
                risk_action=decision.action.value, approved_qty=decision.approved_qty,
                reason=f"{signal.reason} | {decision.reason}",
            ))
            if decision.action == RiskAction.REJECT or decision.approved_qty <= 0:
                continue
            # Queue to fill at the symbol's NEXT bar open
            self._pending[signal.symbol] = _PendingOrder(
                side=signal.side,
                qty=decision.approved_qty,
                stop_pct=signal.stop_pct,
                target_pct=signal.target_pct,
                reason=signal.reason,
                trailing_stop_pct=signal.trailing_stop_pct,
                trailing_atr_mult=signal.trailing_atr_mult,
            )

    def _generate_orders_batch(self, bars: list[BarEvent]) -> None:
        """Cross-sectional order generation: one on_bars() call over the whole timestamp.

        Mirrors _generate_orders' risk-halt bookkeeping and queueing, but computes the halt
        state once (all bars share a timestamp) and resolves each signal's ref price from its
        own symbol's close.
        """
        eq = self.portfolio.equity()
        ts = bars[0].ts
        day_return_pct, current_drawdown = self._update_halt_state(eq, ts.date())

        price_by_symbol = {b.symbol: b.close for b in bars}
        for signal in self.strategy.on_bars(bars, self.portfolio):
            ref_price = price_by_symbol.get(signal.symbol, 0.0)
            decision = self.risk.evaluate(
                signal, self.portfolio, ref_price=ref_price,
                day_return_pct=day_return_pct, current_drawdown=current_drawdown,
            )
            self.signals.append(SignalAudit(
                ts=ts, symbol=signal.symbol, side=signal.side.value,
                risk_action=decision.action.value, approved_qty=decision.approved_qty,
                reason=f"{signal.reason} | {decision.reason}",
            ))
            if decision.action == RiskAction.REJECT or decision.approved_qty <= 0:
                continue
            self._pending[signal.symbol] = _PendingOrder(
                side=signal.side,
                qty=decision.approved_qty,
                stop_pct=signal.stop_pct,
                target_pct=signal.target_pct,
                reason=signal.reason,
                trailing_stop_pct=signal.trailing_stop_pct,
                trailing_atr_mult=signal.trailing_atr_mult,
            )
