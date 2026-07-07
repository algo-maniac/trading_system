"""In-memory LedgerRepo fake for DB-free unit tests (mirrors PostgresLedger semantics)."""
import copy
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from trading_system.execution.ledger import OrderRow, PositionRow


class FakeLedger:
    def __init__(self) -> None:
        self.signals: dict[str, dict] = {}
        self.orders: list[dict] = []
        self.fills: list[dict] = []
        self.positions: list[dict] = []
        self.sleeves: dict[str, dict] = {}
        self.equity_snapshots: list[dict] = []

    @contextmanager
    def atomic(self):
        """Model a DB transaction: snapshot the mutable ledger state on entry and, on
        any exception inside the block, restore it — so tests see the same all-or-nothing
        rollback PostgresLedger.atomic() gives via psycopg's transaction()."""
        snap = copy.deepcopy((self.orders, self.fills, self.positions, self.sleeves))
        try:
            yield
        except Exception:
            self.orders, self.fills, self.positions, self.sleeves = copy.deepcopy(snap)
            raise

    # --- signals ---
    def insert_signal(self, sig: dict) -> bool:
        if sig["id"] in self.signals:
            return False
        self.signals[sig["id"]] = dict(sig)
        return True

    def get_signal(self, signal_id: str):
        return self.signals.get(signal_id)

    # --- orders ---
    def insert_order(self, order: dict) -> OrderRow:
        for o in self.orders:
            if o["signal_id"] == order["signal_id"]:
                return self._row(o)
        o = {**order, "id": len(self.orders) + 1, "status": "PENDING", "reject_reason": None}
        self.orders.append(o)
        return self._row(o)

    @staticmethod
    def _row(o: dict) -> OrderRow:
        return OrderRow(o["id"], o["signal_id"], o["sleeve"], o["symbol"], o["side"],
                        o["qty"], o["product"], o["status"])

    def set_order_status(self, order_id: int, status: str, reason: str | None = None) -> None:
        for o in self.orders:
            if o["id"] == order_id:
                o["status"] = status
                if reason:
                    o["reject_reason"] = reason

    def pending_orders(self, symbol: str | None = None) -> list[OrderRow]:
        return [self._row(o) for o in self.orders
                if o["status"] == "PENDING" and (symbol is None or o["symbol"] == symbol)]

    # --- fills ---
    def insert_fill(self, order_id: int, ts: datetime, qty: int, price: float,
                    fees: float, slippage: float) -> bool:
        if any(f["order_id"] == order_id for f in self.fills):
            return False
        self.fills.append({"order_id": order_id, "ts": ts, "qty": qty, "price": price,
                           "fees": fees, "slippage": slippage})
        return True

    # --- positions ---
    def open_position(self, sleeve: str, symbol: str) -> Optional[PositionRow]:
        for p in self.positions:
            if p["sleeve"] == sleeve and p["symbol"] == symbol and p["closed_ts"] is None:
                return PositionRow(p["id"], p["sleeve"], p["symbol"], p["qty"], p["avg_price"],
                                   p["entry_fees"], p["entry_slippage"], p["opened_ts"],
                                   p.get("open_signal_id"))
        return None

    def open_positions(self, sleeve: str | None = None) -> list[PositionRow]:
        return [PositionRow(p["id"], p["sleeve"], p["symbol"], p["qty"], p["avg_price"],
                            p["entry_fees"], p["entry_slippage"], p["opened_ts"],
                            p.get("open_signal_id"))
                for p in self.positions
                if p["closed_ts"] is None and (sleeve is None or p["sleeve"] == sleeve)]

    def upsert_open_position(self, sleeve: str, symbol: str, qty: int, avg_price: float,
                             entry_fees: float, entry_slippage: float, opened_ts: datetime,
                             open_signal_id: str | None) -> None:
        for p in self.positions:
            if p["sleeve"] == sleeve and p["symbol"] == symbol and p["opened_ts"] == opened_ts:
                return
        self.positions.append({
            "id": len(self.positions) + 1, "sleeve": sleeve, "symbol": symbol, "qty": qty,
            "avg_price": avg_price, "entry_fees": entry_fees, "entry_slippage": entry_slippage,
            "opened_ts": opened_ts, "closed_ts": None, "realized_pnl": None,
            "open_signal_id": open_signal_id,
        })

    def close_position(self, position_id: int, closed_ts: datetime, realized_pnl: float,
                       exit_reason: str) -> None:
        for p in self.positions:
            if p["id"] == position_id and p["closed_ts"] is None:
                p.update(qty=0, closed_ts=closed_ts, realized_pnl=realized_pnl,
                         exit_reason=exit_reason)

    # --- sleeves / equity ---
    def sleeve_cash(self, sleeve: str) -> float:
        return self.sleeves[sleeve]["cash"]

    def adjust_sleeve_cash(self, sleeve: str, delta: float) -> float:
        self.sleeves[sleeve]["cash"] += delta
        return self.sleeves[sleeve]["cash"]

    def ensure_sleeve(self, sleeve: str, capital: float) -> None:
        self.sleeves.setdefault(sleeve, {"capital": capital, "cash": capital})

    def snapshot_equity(self, ts: datetime, sleeve: str, cash: float,
                        positions_value: float) -> None:
        self.equity_snapshots.append({"ts": ts, "sleeve": sleeve, "cash": cash,
                                      "positions_value": positions_value,
                                      "equity": cash + positions_value})
