"""TraderCore: signal→gate→order path, redelivery idempotency, position protection
(stop/target/trailing/MIS square-off), EOD snapshots — all DB-free via FakeLedger."""
from datetime import datetime, timezone

import pytest

from tests.fakes import FakeLedger
from trading_system.agents.trader.core import SleeveExecution, TraderCore
from trading_system.bus.signals import SignalEvent, signal_id
from trading_system.execution.fees import Product
from trading_system.execution.paper_broker import PaperBroker
from trading_system.risk.models import RiskConfig

_UTC = timezone.utc


def _bar_at(day, hh, mm, *, o, h=None, lo=None, c=None, symbol="RELIANCE", interval="day",
            features=None):
    from trading_system.replay.events import BarEvent
    c = c if c is not None else o
    return BarEvent(
        ts=datetime(2026, 7, day, hh, mm, tzinfo=_UTC), symbol=symbol, interval=interval,
        instrument_token=1, open=o, high=h if h is not None else max(o, c),
        low=lo if lo is not None else min(o, c), close=c, volume=10_000,
        features=features or {},
    )


def _core(product=Product.CNC):
    ledger = FakeLedger()
    broker = PaperBroker(ledger, slippage_bps=0.0, fees_enabled=False)
    core = TraderCore(
        ledger, broker,
        sleeves=[SleeveExecution(name="momentum", product=product,
                                 risk=RiskConfig(max_position_pct=1.0, max_positions=25))],
        paper_capital=1_000_000.0,
        sleeve_weights={"momentum": 1.0},
    )
    return core, ledger


def _entry_signal(sid="s1", *, size=0.5, stop_pct=None, target_pct=None,
                  trailing_stop_pct=None):
    return SignalEvent(
        id=sid, ts=datetime(2026, 7, 1, tzinfo=_UTC), sleeve="momentum",
        strategy="xsec_momentum", symbol="RELIANCE", side="BUY", size_hint=size,
        stop_pct=stop_pct, target_pct=target_pct, trailing_stop_pct=trailing_stop_pct,
        reason="test_entry", ref_price=2000.0,
    )


def test_signal_gate_place_fill_roundtrip():
    core, ledger = _core()
    assert core.on_signal(_entry_signal()) == "placed"
    core.on_bar(_bar_at(2, 9, 30, o=2000.0))
    pos = ledger.open_position("momentum", "RELIANCE")
    assert pos is not None and pos.qty == 250  # 50% of 1M at 2000
    assert ledger.sleeve_cash("momentum") == pytest.approx(500_000)


def test_redelivered_signal_places_one_order():
    core, ledger = _core()
    core.on_signal(_entry_signal("dup"))
    core.on_signal(_entry_signal("dup"))
    assert len(ledger.orders) == 1


def test_sell_signal_without_position_rejected():
    core, ledger = _core()
    sig = SignalEvent(id="x", ts=datetime(2026, 7, 1, tzinfo=_UTC), sleeve="momentum",
                      strategy="s", symbol="RELIANCE", side="SELL", ref_price=2000.0)
    assert core.on_signal(sig).startswith("rejected")
    assert ledger.orders == []


def test_fixed_stop_triggers_protective_exit_next_bar():
    core, ledger = _core()
    core.on_signal(_entry_signal(stop_pct=0.05))          # stop 5% below entry
    core.on_bar(_bar_at(2, 9, 30, o=2000.0))              # fill @2000
    core.on_bar(_bar_at(3, 9, 30, o=1980.0, lo=1880.0))   # low breaches 1900 stop
    sells = [o for o in ledger.orders if o["side"] == "SELL"]
    assert len(sells) == 1
    core.on_bar(_bar_at(4, 9, 30, o=1890.0))              # exit fills next bar
    assert ledger.open_position("momentum", "RELIANCE") is None
    closed = [p for p in ledger.positions if p["closed_ts"] is not None]
    assert closed and closed[0]["realized_pnl"] == pytest.approx(250 * (1890 - 2000))


def test_protective_exit_fires_once_under_bar_redelivery():
    core, ledger = _core()
    core.on_signal(_entry_signal(stop_pct=0.05))
    core.on_bar(_bar_at(2, 9, 30, o=2000.0))
    crash = _bar_at(3, 9, 30, o=1980.0, lo=1880.0)
    core.on_bar(crash)
    core.on_bar(crash)  # redelivered bar — pending SELL blocks a duplicate
    assert len([o for o in ledger.orders if o["side"] == "SELL"]) == 1


def test_target_triggers_exit():
    core, ledger = _core()
    core.on_signal(_entry_signal(target_pct=0.10))
    core.on_bar(_bar_at(2, 9, 30, o=2000.0))
    core.on_bar(_bar_at(3, 9, 30, o=2150.0, h=2250.0))    # high clears 2200 target
    assert [o for o in ledger.orders if o["side"] == "SELL"]


def test_trailing_stop_ratchets_off_peak():
    core, ledger = _core()
    core.on_signal(_entry_signal(trailing_stop_pct=0.05))
    core.on_bar(_bar_at(2, 9, 30, o=2000.0))
    core.on_bar(_bar_at(3, 9, 30, o=2300.0, h=2400.0))    # peak ratchets to 2400
    core.on_bar(_bar_at(4, 9, 30, o=2350.0, lo=2270.0))   # 2400*0.95=2280 breached
    assert [o for o in ledger.orders if o["side"] == "SELL"]


def test_mis_square_off_at_cutoff():
    core, ledger = _core(product=Product.MIS)
    core.on_signal(_entry_signal(size=0.2))
    core.on_bar(_bar_at(2, 9, 35, o=2000.0, interval="5minute"))
    core.on_bar(_bar_at(2, 15, 15, o=2010.0, interval="5minute"))   # cutoff reached
    sells = [o for o in ledger.orders if o["side"] == "SELL"]
    assert len(sells) == 1
    core.on_bar(_bar_at(2, 15, 20, o=2012.0, interval="5minute"))   # square-off fills
    assert ledger.open_position("momentum", "RELIANCE") is None


def test_snapshot_writes_equity_rows():
    core, ledger = _core()
    core.on_signal(_entry_signal())
    core.on_bar(_bar_at(2, 9, 30, o=2000.0))
    core.snapshot(datetime(2026, 7, 2, 15, 30, tzinfo=_UTC))
    assert len(ledger.equity_snapshots) == 1
    assert ledger.equity_snapshots[0]["equity"] == pytest.approx(1_000_000)
