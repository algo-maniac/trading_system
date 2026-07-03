"""TraderCore: signal → risk gate → order; bar → fills + position protection.

Responsibility split with the Strategist (mirrors the backtest engine's split):
  - strategies own RULE exits (trend-break, max_hold, xsec drops) — those arrive as
    SELL SignalEvents like any other signal;
  - the Trader owns PROTECTIVE exits, which in backtests were engine policies:
    fixed stop_pct / target_pct, trailing stops (pct or k×ATR off the running peak),
    and the MIS end-of-day square-off. Protection params ride on the entry signal and
    are journaled with it, so a restarted Trader re-derives them from the ledger.

Differences from the backtest engine, accepted for the paper phase and journaled:
  - protective exits fill at the NEXT bar's open via the normal pending-order path
    (the engine fills stops intra-bar at the stop price). Slightly worse fills →
    paper results are conservative, not flattering.
  - trailing-stop peaks are tracked in memory and re-seeded from entry price after a
    restart (the stop re-arms lower; conservative for the position, never unsafe).

Everything is idempotent: signals/orders dedupe on deterministic ids; a protective
exit's id derives from the position's opening signal, so redelivered bars can't
double-fire; a pending SELL for a (sleeve, symbol) blocks further protective SELLs.
"""
from dataclasses import dataclass, field
from datetime import datetime, time

from trading_system.bus.signals import SignalEvent, signal_id
from trading_system.execution.broker import Broker, OrderRequest
from trading_system.execution.fees import Product
from trading_system.execution.ledger import LedgerRepo
from trading_system.observability import get_logger
from trading_system.portfolio.live import LivePortfolio
from trading_system.replay.events import BarEvent
from trading_system.risk.manager import RiskManager
from trading_system.risk.models import RiskAction, RiskConfig
from trading_system.strategy.signals import Side, Signal

logger = get_logger(__name__)

_MIS_SQUARE_OFF = time(15, 15)


@dataclass
class SleeveExecution:
    """Per-sleeve execution config: product type + risk limits sized for the sleeve."""
    name: str
    product: Product = Product.CNC
    risk: RiskConfig = field(default_factory=RiskConfig)


class TraderCore:
    def __init__(
        self,
        ledger: LedgerRepo,
        broker: Broker,
        sleeves: list[SleeveExecution],
        paper_capital: float,
        sleeve_weights: dict[str, float],
    ) -> None:
        self._ledger = ledger
        self._broker = broker
        self._sleeves = {s.name: s for s in sleeves}
        self._risk = {s.name: RiskManager(s.risk) for s in sleeves}
        self._views = {s.name: LivePortfolio(ledger, s.name) for s in sleeves}
        for name in self._sleeves:
            weight = sleeve_weights.get(name, 0.0)
            ledger.ensure_sleeve(name, paper_capital * weight)
        # trailing-stop peaks: (sleeve, symbol) -> running peak price
        self._peaks: dict[tuple[str, str], float] = {}
        # Liveness counters — see agents/heartbeat.py; a quiet Trader (no signals to
        # gate) looks identical to a stuck one without these.
        self.bars_processed = 0
        self.signals_handled = 0

    # --- signal path ------------------------------------------------------------

    def on_signal(self, sig: SignalEvent) -> str:
        """Journal + gate + place. Returns the outcome for logging/tests."""
        self.signals_handled += 1
        sleeve = self._sleeves.get(sig.sleeve)
        if sleeve is None:
            logger.warning("signal_unknown_sleeve", sleeve=sig.sleeve, sid=sig.id[:12])
            return "unknown_sleeve"
        self._ledger.insert_signal({
            "id": sig.id, "ts": sig.ts, "sleeve": sig.sleeve, "strategy": sig.strategy,
            "symbol": sig.symbol, "side": sig.side, "size_hint": sig.size_hint,
            "stop_pct": sig.stop_pct, "target_pct": sig.target_pct,
            "trailing_stop_pct": sig.trailing_stop_pct,
            "trailing_atr_mult": sig.trailing_atr_mult,
            "reason": sig.reason, "features": sig.features,
        })
        view = self._views[sig.sleeve]
        decision = self._risk[sig.sleeve].evaluate(
            Signal(symbol=sig.symbol, side=Side(sig.side), reason=sig.reason,
                   size_hint=sig.size_hint or 0.0),
            view,  # type: ignore[arg-type] — LivePortfolio provides the surface evaluate uses
            ref_price=sig.ref_price,
        )
        if decision.action == RiskAction.REJECT or decision.approved_qty <= 0:
            logger.info("signal_rejected", sid=sig.id[:12], symbol=sig.symbol,
                        reason=decision.reason)
            return f"rejected: {decision.reason}"
        self._broker.place(OrderRequest(
            signal_id=sig.id, sleeve=sig.sleeve, symbol=sig.symbol, side=sig.side,
            qty=decision.approved_qty, product=sleeve.product,
        ))
        logger.info("order_placed", sid=sig.id[:12], symbol=sig.symbol, side=sig.side,
                    qty=decision.approved_qty)
        return "placed"

    # --- bar path ----------------------------------------------------------------

    def on_bar(self, bar: BarEvent) -> None:
        self.bars_processed += 1
        for view in self._views.values():
            view.update_price(bar.symbol, bar.close, bar.ts)
        fills = self._broker.on_bar(bar)  # type: ignore[attr-defined] — paper broker
        for _order_id, fill in fills:
            if fill.side == "SELL":
                self._peaks.pop((self._fill_sleeve(fill.symbol), fill.symbol), None)
        self._protect_positions(bar)

    def _fill_sleeve(self, symbol: str) -> str:
        for name in self._sleeves:
            if self._ledger.open_position(name, symbol) is not None:
                return name
        return ""

    def _protect_positions(self, bar: BarEvent) -> None:
        for name, sleeve in self._sleeves.items():
            pos = self._ledger.open_position(name, bar.symbol)
            if pos is None or pos.opened_ts == bar.ts:
                continue  # nothing held, or the fill happened on this very bar
            if self._has_pending_sell(name, bar.symbol):
                continue
            entry_sig: dict = {}
            if pos.open_signal_id:
                entry_sig = self._ledger.get_signal(pos.open_signal_id) or {}
            reason = self._exit_reason(sleeve, pos.avg_price, entry_sig, bar, name)
            if reason is None:
                continue
            exit_id = signal_id(name, "protect", bar.symbol, pos.opened_ts, f"SELL-{reason}")
            self._ledger.insert_signal({
                "id": exit_id, "ts": bar.ts, "sleeve": name, "strategy": "protect",
                "symbol": bar.symbol, "side": "SELL", "size_hint": None, "stop_pct": None,
                "target_pct": None, "trailing_stop_pct": None, "trailing_atr_mult": None,
                "reason": reason, "features": {},
            })
            self._broker.place(OrderRequest(
                signal_id=exit_id, sleeve=name, symbol=bar.symbol, side="SELL",
                qty=pos.qty, product=sleeve.product,
            ))
            logger.info("protective_exit", sleeve=name, symbol=bar.symbol, reason=reason)

    def _has_pending_sell(self, sleeve: str, symbol: str) -> bool:
        return any(o.side == "SELL" and o.sleeve == sleeve
                   for o in self._ledger.pending_orders(symbol=symbol))

    def _exit_reason(self, sleeve: SleeveExecution, entry: float,
                     entry_sig: dict, bar: BarEvent, name: str) -> str | None:
        # MIS square-off: flat before the close, no overnight carry — engine parity.
        if sleeve.product == Product.MIS and bar.ts.time() >= _MIS_SQUARE_OFF:
            return "eod_squareoff"
        stop_pct = entry_sig.get("stop_pct")
        if stop_pct and bar.low <= entry * (1.0 - float(stop_pct)):
            return "stop_loss"
        target_pct = entry_sig.get("target_pct")
        if target_pct and bar.high >= entry * (1.0 + float(target_pct)):
            return "target"
        key = (name, bar.symbol)
        peak = max(self._peaks.get(key, entry), bar.high)
        self._peaks[key] = peak
        trailing_pct = entry_sig.get("trailing_stop_pct")
        if trailing_pct and bar.low <= peak * (1.0 - float(trailing_pct)):
            return "trailing_stop"
        trailing_atr = entry_sig.get("trailing_atr_mult")
        atr = bar.features.get("atr_14")
        if trailing_atr and atr and bar.low <= peak - float(trailing_atr) * float(atr):
            return "trailing_atr_stop"
        return None

    # --- EOD ----------------------------------------------------------------------

    def snapshot(self, ts: datetime) -> None:
        """Mark-to-market equity snapshot per sleeve (EOD job + on shutdown)."""
        for name, view in self._views.items():
            self._ledger.snapshot_equity(ts, name, view.cash, view.positions_value())
            logger.info("equity_snapshot", sleeve=name, equity=round(view.equity(), 2))
