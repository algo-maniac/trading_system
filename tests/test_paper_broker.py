"""PaperBroker: next-bar-open fills, ledger updates, idempotency under redelivery."""
from datetime import datetime, timezone

import pytest

from tests.fakes import FakeLedger
from trading_system.execution.broker import OrderRequest
from trading_system.execution.fees import Product
from trading_system.execution.paper_broker import PaperBroker
from trading_system.replay.events import BarEvent

_UTC = timezone.utc


def _bar(day, open_, symbol="RELIANCE"):
    return BarEvent(
        ts=datetime(2026, 7, day, 9, 30, tzinfo=_UTC), symbol=symbol, interval="day",
        instrument_token=1, open=open_, high=open_ + 1, low=open_ - 1, close=open_,
        volume=10_000, features={},
    )


def _broker(fees=False):
    ledger = FakeLedger()
    ledger.ensure_sleeve("momentum", 1_000_000.0)
    return PaperBroker(ledger, slippage_bps=0.0, fees_enabled=fees), ledger


def _buy(signal_id="sig-1", qty=100):
    return OrderRequest(signal_id=signal_id, sleeve="momentum", symbol="RELIANCE",
                        side="BUY", qty=qty, product=Product.CNC)


def test_place_then_fill_at_next_bar_open_updates_ledger():
    broker, ledger = _broker()
    placed = broker.place(_buy())
    assert placed.status == "PENDING"
    fills = broker.on_bar(_bar(3, open_=2000.0))
    assert len(fills) == 1
    _, fill = fills[0]
    assert fill.price == pytest.approx(2000.0)
    assert ledger.sleeve_cash("momentum") == pytest.approx(1_000_000 - 200_000)
    pos = ledger.open_position("momentum", "RELIANCE")
    assert pos is not None and pos.qty == 100 and pos.avg_price == pytest.approx(2000.0)
    assert ledger.orders[0]["status"] == "FILLED"


def test_place_is_idempotent_on_signal_id():
    broker, ledger = _broker()
    broker.place(_buy("dup"))
    broker.place(_buy("dup"))
    assert len(ledger.orders) == 1


def test_redelivered_bar_does_not_double_fill():
    broker, ledger = _broker()
    broker.place(_buy())
    broker.on_bar(_bar(3, open_=2000.0))
    again = broker.on_bar(_bar(3, open_=2000.0))  # bus redelivery of the same bar
    assert again == []
    assert len(ledger.fills) == 1
    assert ledger.sleeve_cash("momentum") == pytest.approx(800_000)


def test_sell_closes_position_and_realizes_pnl():
    broker, ledger = _broker()
    broker.place(_buy("open-sig"))
    broker.on_bar(_bar(3, open_=2000.0))
    broker.place(OrderRequest(signal_id="close-sig", sleeve="momentum", symbol="RELIANCE",
                              side="SELL", qty=100, product=Product.CNC))
    broker.on_bar(_bar(10, open_=2100.0))
    assert ledger.open_position("momentum", "RELIANCE") is None
    closed = ledger.positions[0]
    assert closed["realized_pnl"] == pytest.approx(100 * (2100 - 2000))
    assert ledger.sleeve_cash("momentum") == pytest.approx(1_000_000 + 10_000)


def test_poll_fills_drains_queue_once():
    broker, _ = _broker()
    broker.place(_buy())
    broker.on_bar(_bar(3, open_=2000.0))
    now = datetime(2026, 7, 3, tzinfo=_UTC)
    assert len(broker.poll_fills(now)) == 1
    assert broker.poll_fills(now) == []


def test_fees_reduce_cash_when_enabled():
    broker, ledger = _broker(fees=True)
    broker.place(_buy())
    broker.on_bar(_bar(3, open_=2000.0))
    assert ledger.sleeve_cash("momentum") < 1_000_000 - 200_000  # fees on top of notional
