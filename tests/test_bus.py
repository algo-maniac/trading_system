"""Bus: BarEvent serde identity + Redis Streams publish/poll/ack/replay (fakeredis-backed)."""
from datetime import UTC, datetime

import fakeredis
import pytest

from trading_system.bus.redis_streams import RedisBarBus
from trading_system.bus.serde import bar_to_fields, fields_to_bar
from trading_system.replay.events import BarEvent

_UTC = UTC


def _bar(i: int = 0, sym: str = "RELIANCE") -> BarEvent:
    return BarEvent(
        ts=datetime(2024, 1, 2, 9, 15 + i, tzinfo=_UTC), symbol=sym, interval="5minute",
        instrument_token=12345, open=100.1 + i, high=101.25 + i, low=99.9 + i,
        close=100.75 + i, volume=5000 + i, features={"vwap": 100.4 + i, "atr_14": 1.2},
    )


@pytest.fixture
def bus() -> RedisBarBus:
    fr = fakeredis.FakeRedis(decode_responses=True)
    return RedisBarBus(client=fr, bar_stream="test.bars", alert_stream="test.alerts", maxlen=1000)


def test_serde_round_trip_identity():
    b = _bar()
    rt = fields_to_bar(bar_to_fields(b))
    assert rt == b                       # ts/symbol/interval/token/OHLCV all equal
    assert rt.features == b.features     # features survive the JSON hop
    assert rt.open == b.open and rt.close == b.close  # floats exact via repr()


def test_publish_then_poll_in_order(bus):
    bus.ensure_group("g", start_id="0")  # consume from the beginning of the stream
    bars = [_bar(i) for i in range(4)]
    for b in bars:
        bus.publish(b)
    got = bus.poll("g", "c", block_ms=10)
    assert [x for _, x in got] == bars   # delivered in publish order, reconstructed exactly


def test_ack_drains_pending_then_replay(bus):
    bus.ensure_group("g", start_id="0")
    bus.publish(_bar())
    got = bus.poll("g", "c", block_ms=10)
    assert len(got) == 1
    # delivered-but-un-acked is visible to a pending re-read (crash recovery)
    assert len(bus.read_pending("g", "c")) == 1
    bus.ack("g", got[0][0])
    assert bus.read_pending("g", "c") == []


def test_live_group_skips_bars_published_before_it(bus):
    # A group created at "$" (live-only) does not see history published before its creation.
    bus.publish(_bar())
    bus.ensure_group("g", start_id="$")
    assert bus.poll("g", "c", block_ms=10) == []


def test_publish_alert_writes_to_alert_stream(bus):
    msg_id = bus.publish_alert("token_expired", "no token for today")
    assert msg_id
    entries = bus._r.xrange("test.alerts")
    assert len(entries) == 1
    assert entries[0][1]["kind"] == "token_expired"


# --- SignalEvent serde ---

def test_signal_serde_roundtrip_identity():
    from datetime import datetime, timezone

    from trading_system.bus.signals import (
        SignalEvent, fields_to_signal, signal_id, signal_to_fields,
    )

    sig = SignalEvent(
        id=signal_id("momentum", "xsec_momentum", "RELIANCE",
                     datetime(2026, 7, 3, 9, 30, tzinfo=timezone.utc), "BUY"),
        ts=datetime(2026, 7, 3, 9, 30, tzinfo=timezone.utc),
        sleeve="momentum", strategy="xsec_momentum", symbol="RELIANCE", side="BUY",
        size_hint=0.05, stop_pct=None, target_pct=0.1234567891011,
        trailing_stop_pct=None, trailing_atr_mult=3.0,
        reason="xsec_entry top20", features={"momentum_252_21": 0.42}, ref_price=2914.35,
    )
    assert fields_to_signal(signal_to_fields(sig)) == sig


def test_signal_id_deterministic():
    from datetime import datetime, timezone

    from trading_system.bus.signals import signal_id

    ts = datetime(2026, 7, 3, 9, 30, tzinfo=timezone.utc)
    a = signal_id("s", "strat", "SYM", ts, "BUY")
    assert a == signal_id("s", "strat", "SYM", ts, "BUY")
    assert a != signal_id("s", "strat", "SYM", ts, "SELL")
