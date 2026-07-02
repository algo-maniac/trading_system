"""Reader agent: token self-heal + scheduled ingest/publish cycle (fakeredis, no Kite/DB)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import fakeredis
import pytest
from kiteconnect.exceptions import TokenException

from trading_system.agents.reader.scheduler import ReaderScheduler
from trading_system.agents.reader.token_manager import TokenManager
from trading_system.bus.redis_streams import RedisBarBus
from trading_system.replay.events import BarEvent

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def bus() -> RedisBarBus:
    fr = fakeredis.FakeRedis(decode_responses=True)
    return RedisBarBus(client=fr, bar_stream="t.bars", alert_stream="t.alerts", maxlen=1000)


def _alert_kinds(bus: RedisBarBus) -> list[str]:
    return [fields["kind"] for _id, fields in bus._r.xrange("t.alerts")]


# --- TokenManager ---

def test_guard_alerts_and_pauses_on_token_exception(bus):
    tm = TokenManager(alert_bus=bus)

    def boom():
        raise TokenException("access token expired")

    assert tm.guard(boom) is None              # paused, not crashed
    assert _alert_kinds(bus) == ["token_expired"]


def test_guard_passes_through_on_success(bus):
    tm = TokenManager(alert_bus=bus)
    assert tm.guard(lambda: 42) == 42
    assert _alert_kinds(bus) == []             # no spurious alert


def test_guard_reraises_non_token_errors(bus):
    tm = TokenManager(alert_bus=bus)

    def bug():
        raise ValueError("a real bug, not a token problem")

    with pytest.raises(ValueError):
        tm.guard(bug)                          # genuine errors must surface, not be swallowed


# --- ReaderScheduler.run_cycle ---

def _fake_bars(symbols, intervals, d0, d1):
    return [
        BarEvent(ts=datetime(2024, 1, 2, 9, 15 + i), symbol="RELIANCE", interval="5minute",
                 instrument_token=1, open=100 + i, high=101 + i, low=99 + i, close=100.5 + i,
                 volume=1000 + i, features={})
        for i in range(3)
    ]


def _scheduler(bus, *, ingest_fn=None, read_bars_fn=_fake_bars):
    return ReaderScheduler(
        bus, TokenManager(alert_bus=bus),
        symbols=["RELIANCE"], intervals=["5minute"], cron="5 * * * *",
        ingest_fn=ingest_fn or (lambda *a, **k: []),
        read_bars_fn=read_bars_fn,
    )


def test_cycle_publishes_new_bars_then_dedups(bus):
    s = _scheduler(bus)
    now = datetime(2024, 1, 2, 10, 5, tzinfo=IST)   # Tue, in session
    r1 = s.run_cycle(now=now)
    assert r1 == {"status": "ok", "published": 3}
    # same bars next cycle → cursor suppresses re-publish
    assert s.run_cycle(now=now)["published"] == 0


def test_cycle_skips_non_trading_day(bus):
    s = _scheduler(bus)
    r = s.run_cycle(now=datetime(2024, 1, 26, 10, 5, tzinfo=IST))  # Republic Day holiday
    assert r["status"] == "skipped_non_trading_day"
    assert r["published"] == 0


def test_cycle_skips_off_hours(bus):
    s = _scheduler(bus)
    r = s.run_cycle(now=datetime(2024, 1, 2, 17, 0, tzinfo=IST))   # after close
    assert r["status"] == "skipped_off_hours"


def test_cycle_pauses_and_alerts_on_token_expiry(bus):
    def boom(*a, **k):
        raise TokenException("access token expired")

    s = _scheduler(bus, ingest_fn=boom)
    r = s.run_cycle(now=datetime(2024, 1, 2, 10, 5, tzinfo=IST))
    assert r == {"status": "paused_no_token", "published": 0}
    assert "token_expired" in _alert_kinds(bus)
