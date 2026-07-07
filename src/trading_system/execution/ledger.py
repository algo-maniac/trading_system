"""Persistent paper-trading ledger (live_* tables) behind a repository protocol.

The PaperBroker and Trader agent speak `LedgerRepo`; production uses `PostgresLedger`,
tests inject an in-memory fake. All mutations are single-statement idempotent writes —
the at-least-once bus can redeliver any event and the ledger stays correct:
  - orders are unique per signal_id (insert_order returns the existing row on conflict)
  - fills are unique per order_id (one fill per paper order, whole-qty model)
  - position opens are unique per (sleeve, symbol, opened_ts)
"""
import json
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import psycopg

from trading_system.observability import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class OrderRow:
    id: int
    signal_id: str
    sleeve: str
    symbol: str
    side: str
    qty: int
    product: str
    status: str


@dataclass(frozen=True)
class PositionRow:
    id: int
    sleeve: str
    symbol: str
    qty: int
    avg_price: float
    entry_fees: float
    entry_slippage: float
    opened_ts: datetime
    open_signal_id: str | None = None


class LedgerRepo(Protocol):
    def atomic(self) -> AbstractContextManager[None]: ...     # all-or-nothing mutation group
    def insert_signal(self, sig: dict) -> bool: ...           # type: ignore[type-arg]
    def get_signal(self, signal_id: str) -> dict | None: ...  # type: ignore[type-arg]
    def insert_order(self, order: dict) -> OrderRow: ...      # type: ignore[type-arg]
    def set_order_status(self, order_id: int, status: str, reason: str | None = None) -> None: ...
    def pending_orders(self, symbol: str | None = None) -> list[OrderRow]: ...
    def insert_fill(self, order_id: int, ts: datetime, qty: int, price: float,
                    fees: float, slippage: float) -> bool: ...
    def open_position(self, sleeve: str, symbol: str) -> PositionRow | None: ...
    def open_positions(self, sleeve: str | None = None) -> list[PositionRow]: ...
    def upsert_open_position(self, sleeve: str, symbol: str, qty: int, avg_price: float,
                             entry_fees: float, entry_slippage: float, opened_ts: datetime,
                             open_signal_id: str | None) -> None: ...
    def close_position(self, position_id: int, closed_ts: datetime, realized_pnl: float,
                       exit_reason: str) -> None: ...
    def sleeve_cash(self, sleeve: str) -> float: ...
    def adjust_sleeve_cash(self, sleeve: str, delta: float) -> float: ...
    def ensure_sleeve(self, sleeve: str, capital: float) -> None: ...
    def snapshot_equity(self, ts: datetime, sleeve: str, cash: float,
                        positions_value: float) -> None: ...


class PostgresLedger:
    """LedgerRepo over the live_* tables. One connection, autocommit per statement."""

    def __init__(self, conn: psycopg.Connection) -> None:  # type: ignore[type-arg]
        self._conn = conn
        self._conn.autocommit = True

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Run several mutations as ONE all-or-nothing transaction despite autocommit.

        psycopg issues an explicit BEGIN/COMMIT for transaction() even in autocommit
        mode (and ROLLBACK on any exception), so a crash partway through a fill
        application leaves the ledger untouched instead of half-written — the
        redelivered bar then re-applies the whole fill cleanly (the fill-insert
        idempotency gate only protects a COMMITTED fill, never a partial one)."""
        with self._conn.transaction():
            yield

    def insert_signal(self, sig: dict) -> bool:  # type: ignore[type-arg]
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO live_signals (id, ts, sleeve, strategy, symbol, side, size_hint,
                    stop_pct, target_pct, trailing_stop_pct, trailing_atr_mult, reason, features)
                VALUES (%(id)s, %(ts)s, %(sleeve)s, %(strategy)s, %(symbol)s, %(side)s,
                    %(size_hint)s, %(stop_pct)s, %(target_pct)s, %(trailing_stop_pct)s,
                    %(trailing_atr_mult)s, %(reason)s, %(features)s)
                ON CONFLICT (id) DO NOTHING
                """,
                {**sig, "features": json.dumps(sig.get("features") or {})},
            )
            return cur.rowcount > 0

    def get_signal(self, signal_id: str) -> dict | None:  # type: ignore[type-arg]
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, ts, sleeve, strategy, symbol, side, size_hint, stop_pct, target_pct, "
                "trailing_stop_pct, trailing_atr_mult, reason FROM live_signals WHERE id=%s",
                (signal_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            keys = ("id", "ts", "sleeve", "strategy", "symbol", "side", "size_hint", "stop_pct",
                    "target_pct", "trailing_stop_pct", "trailing_atr_mult", "reason")
            return dict(zip(keys, row, strict=True))

    def insert_order(self, order: dict) -> OrderRow:  # type: ignore[type-arg]
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO live_orders
                    (signal_id, symbol, side, qty, product, broker, sleeve, status)
                VALUES (%(signal_id)s, %(symbol)s, %(side)s, %(qty)s, %(product)s,
                        %(broker)s, %(sleeve)s, 'PENDING')
                ON CONFLICT (signal_id) DO NOTHING
                RETURNING id, signal_id, sleeve, symbol, side, qty, product, status
                """,
                order,
            )
            row = cur.fetchone()
            if row is None:  # redelivery — return the existing order
                cur.execute(
                    "SELECT id, signal_id, sleeve, symbol, side, qty, product, status "
                    "FROM live_orders WHERE signal_id = %s", (order["signal_id"],),
                )
                row = cur.fetchone()
            return OrderRow(*row)

    def set_order_status(self, order_id: int, status: str, reason: str | None = None) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE live_orders SET status=%s, reject_reason=COALESCE(%s, reject_reason), "
                "updated_at=NOW() WHERE id=%s",
                (status, reason, order_id),
            )

    def pending_orders(self, symbol: str | None = None) -> list[OrderRow]:
        q = ("SELECT id, signal_id, sleeve, symbol, side, qty, product, status "
             "FROM live_orders WHERE status='PENDING'")
        args: tuple = ()  # type: ignore[type-arg]
        if symbol:
            q += " AND symbol=%s"
            args = (symbol,)
        with self._conn.cursor() as cur:
            cur.execute(q + " ORDER BY id", args)
            return [OrderRow(*r) for r in cur.fetchall()]

    def insert_fill(self, order_id: int, ts: datetime, qty: int, price: float,
                    fees: float, slippage: float) -> bool:
        with self._conn.cursor() as cur:
            # one fill per order (whole-qty model) — redelivery no-ops
            cur.execute("SELECT 1 FROM live_fills WHERE order_id=%s", (order_id,))
            if cur.fetchone():
                return False
            cur.execute(
                "INSERT INTO live_fills (order_id, ts, qty, price, fees, slippage) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (order_id, ts, qty, price, json.dumps({"total": fees}), slippage),
            )
            return True

    _POS_COLS = ("id, sleeve, symbol, qty, avg_price, entry_fees, entry_slippage, "
                 "opened_ts, open_signal_id")

    @classmethod
    def _pos_row(cls, r) -> PositionRow:
        return PositionRow(r[0], r[1], r[2], r[3], float(r[4]), r[5], r[6], r[7], r[8])

    def open_position(self, sleeve: str, symbol: str) -> PositionRow | None:
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._POS_COLS} FROM live_positions "
                "WHERE sleeve=%s AND symbol=%s AND closed_ts IS NULL",
                (sleeve, symbol),
            )
            row = cur.fetchone()
            return self._pos_row(row) if row else None

    def open_positions(self, sleeve: str | None = None) -> list[PositionRow]:
        q = f"SELECT {self._POS_COLS} FROM live_positions WHERE closed_ts IS NULL"
        args: tuple = ()  # type: ignore[type-arg]
        if sleeve:
            q += " AND sleeve=%s"
            args = (sleeve,)
        with self._conn.cursor() as cur:
            cur.execute(q + " ORDER BY id", args)
            return [self._pos_row(r) for r in cur.fetchall()]

    def upsert_open_position(self, sleeve: str, symbol: str, qty: int, avg_price: float,
                             entry_fees: float, entry_slippage: float, opened_ts: datetime,
                             open_signal_id: str | None) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO live_positions (sleeve, symbol, qty, avg_price, entry_fees,
                    entry_slippage, opened_ts, open_signal_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (sleeve, symbol, opened_ts) DO NOTHING
                """,
                (sleeve, symbol, qty, avg_price, entry_fees, entry_slippage,
                 opened_ts, open_signal_id),
            )

    def close_position(self, position_id: int, closed_ts: datetime, realized_pnl: float,
                       exit_reason: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE live_positions SET qty=0, closed_ts=%s, realized_pnl=%s, exit_reason=%s "
                "WHERE id=%s AND closed_ts IS NULL",
                (closed_ts, realized_pnl, exit_reason, position_id),
            )

    def sleeve_cash(self, sleeve: str) -> float:
        with self._conn.cursor() as cur:
            cur.execute("SELECT cash FROM live_sleeves WHERE sleeve=%s", (sleeve,))
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"sleeve '{sleeve}' not initialised — call ensure_sleeve first")
            return float(row[0])

    def adjust_sleeve_cash(self, sleeve: str, delta: float) -> float:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE live_sleeves SET cash = cash + %s, updated_at=NOW() "
                "WHERE sleeve=%s RETURNING cash",
                (delta, sleeve),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"sleeve '{sleeve}' not initialised")
            return float(row[0])

    def ensure_sleeve(self, sleeve: str, capital: float) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO live_sleeves (sleeve, capital, cash) VALUES (%s, %s, %s) "
                "ON CONFLICT (sleeve) DO NOTHING",
                (sleeve, capital, capital),
            )

    def snapshot_equity(self, ts: datetime, sleeve: str, cash: float,
                        positions_value: float) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO live_equity (ts, sleeve, cash, positions_value, equity)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (sleeve, ts) DO UPDATE
                  SET cash=EXCLUDED.cash, positions_value=EXCLUDED.positions_value,
                      equity=EXCLUDED.equity
                """,
                (ts, sleeve, cash, positions_value, cash + positions_value),
            )
