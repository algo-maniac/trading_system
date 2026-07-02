"""Broker protocol: the seam between the Trader agent and any execution venue.

The Trader agent speaks only this interface. Two implementations:
  - PaperBroker  — simulated fills against live bar prices, Postgres-backed ledger
                   (Phase 5a; the only one that ever runs unattended).
  - KiteBroker   — real Zerodha orders. Written later, triple-locked off
                   (settings flag + env arm + notional cap) until a paper track
                   record exists.

An `OrderRequest` is an *approved* order — RiskManager.evaluate() has already sized it.
Placement is asynchronous in spirit: place() returns a broker order id; fills are
reported via poll_fills() so the Trader's loop stays single-threaded and testable.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from trading_system.execution.fees import Product
from trading_system.portfolio.models import Fill


@dataclass(frozen=True)
class OrderRequest:
    signal_id: str          # idempotency key — one order per signal, ever
    sleeve: str
    symbol: str
    side: str               # "BUY" | "SELL"
    qty: int
    product: Product = Product.CNC
    meta: dict = field(default_factory=dict)  # type: ignore[type-arg]


@dataclass(frozen=True)
class PlacedOrder:
    order_id: int           # journal id (live_orders.id)
    broker_order_id: str
    status: str             # PENDING | FILLED | REJECTED | CANCELLED


class Broker(Protocol):
    def place(self, order: OrderRequest) -> PlacedOrder:
        """Submit an order. MUST be idempotent on order.signal_id — a redelivered
        signal returns the existing order instead of double-placing."""
        ...

    def poll_fills(self, now: datetime) -> list[tuple[int, Fill]]:
        """Return (order_id, Fill) for orders that filled since the last poll."""
        ...

    def cancel(self, order_id: int) -> bool:
        """Cancel a pending order. Returns True if it was cancelled."""
        ...

    def open_orders(self) -> list[PlacedOrder]:
        ...
