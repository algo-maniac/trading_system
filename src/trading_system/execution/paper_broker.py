"""PaperBroker: simulated execution against live bars, persistent Postgres ledger.

Fill model matches the backtest engine exactly (next-bar-open + adverse slippage bps +
full Zerodha charges via `simulate_fill`) so paper results stay comparable to backtests —
the paper==forward equivalence test is the trust anchor.

Lifecycle:
  place(OrderRequest)         → PENDING row in live_orders (idempotent on signal_id)
  on_bar(BarEvent)            → any PENDING order for that symbol fills at THIS bar's open
                                (the strategist signalled on the previous bar → this is the
                                next-bar-open model), ledger updated atomically-enough:
                                fill insert is the idempotency gate; position/cash follow.
  poll_fills()                → drained queue of (order_id, Fill) for the Trader to notify.

Whole-quantity fills only (liquid names, small paper size). SELLs close the sleeve's open
position and realize P&L net of entry+exit costs, mirroring Portfolio.apply_fill's math.
"""
from datetime import datetime

from trading_system.execution.broker import OrderRequest, PlacedOrder
from trading_system.execution.fees import Product
from trading_system.execution.ledger import LedgerRepo
from trading_system.execution.simulator import simulate_fill
from trading_system.observability import get_logger
from trading_system.portfolio.models import Fill
from trading_system.replay.events import BarEvent

logger = get_logger(__name__)


class PaperBroker:
    def __init__(self, ledger: LedgerRepo, slippage_bps: float = 5.0,
                 fees_enabled: bool = True) -> None:
        self._ledger = ledger
        self._slippage_bps = slippage_bps
        self._fees_enabled = fees_enabled
        self._fill_queue: list[tuple[int, Fill]] = []

    # --- Broker protocol ----------------------------------------------------

    def place(self, order: OrderRequest) -> PlacedOrder:
        row = self._ledger.insert_order({
            "signal_id": order.signal_id,
            "symbol": order.symbol,
            "side": order.side,
            "qty": order.qty,
            "product": order.product.value,
            "broker": "paper",
            "sleeve": order.sleeve,
        })
        if row.status != "PENDING":
            logger.info("paper_place_dedupe", signal_id=order.signal_id, status=row.status)
        return PlacedOrder(order_id=row.id, broker_order_id=f"paper-{row.id}", status=row.status)

    def poll_fills(self, now: datetime) -> list[tuple[int, Fill]]:
        drained, self._fill_queue = self._fill_queue, []
        return drained

    def cancel(self, order_id: int) -> bool:
        self._ledger.set_order_status(order_id, "CANCELLED")
        return True

    def open_orders(self) -> list[PlacedOrder]:
        return [
            PlacedOrder(order_id=r.id, broker_order_id=f"paper-{r.id}", status=r.status)
            for r in self._ledger.pending_orders()
        ]

    # --- Fill engine ----------------------------------------------------------

    def on_bar(self, bar: BarEvent) -> list[tuple[int, Fill]]:
        """Fill any PENDING orders for this symbol at the bar's open. Returns new fills."""
        fills: list[tuple[int, Fill]] = []
        for order in self._ledger.pending_orders(symbol=bar.symbol):
            fill = simulate_fill(
                ts=bar.ts, symbol=bar.symbol, is_buy=(order.side == "BUY"), qty=order.qty,
                clean_price=bar.open, slippage_bps=self._slippage_bps,
                product=Product(order.product), fees_enabled=self._fees_enabled,
            )
            # insert_fill is the idempotency gate: False = already filled (redelivered bar)
            if not self._ledger.insert_fill(
                order.id, fill.ts, fill.qty, fill.price, fill.fees, fill.slippage
            ):
                continue
            self._apply_to_ledger(order, fill)
            self._ledger.set_order_status(order.id, "FILLED")
            fills.append((order.id, fill))
            logger.info(
                "paper_fill", order_id=order.id, sleeve=order.sleeve, symbol=order.symbol,
                side=order.side, qty=order.qty, price=round(fill.price, 2),
                fees=round(fill.fees, 2),
            )
        self._fill_queue.extend(fills)
        return fills

    def _apply_to_ledger(self, order, fill: Fill) -> None:
        sleeve = order.sleeve
        if order.side == "BUY":
            self._ledger.adjust_sleeve_cash(sleeve, -(fill.qty * fill.price + fill.fees))
            self._ledger.upsert_open_position(
                sleeve=sleeve, symbol=order.symbol, qty=fill.qty, avg_price=fill.price,
                entry_fees=fill.fees, entry_slippage=fill.slippage, opened_ts=fill.ts,
                open_signal_id=order.signal_id,
            )
            return
        # SELL: close the sleeve's open position (whole-position exits in this system)
        pos = self._ledger.open_position(sleeve, order.symbol)
        proceeds = fill.qty * fill.price - fill.fees
        self._ledger.adjust_sleeve_cash(sleeve, proceeds)
        if pos is None:
            logger.warning("paper_sell_without_position", sleeve=sleeve, symbol=order.symbol)
            return
        realized = proceeds - (pos.qty * pos.avg_price + pos.entry_fees)
        self._ledger.close_position(
            pos.id, closed_ts=fill.ts, realized_pnl=realized,
            exit_reason=str(order.signal_id),
        )
