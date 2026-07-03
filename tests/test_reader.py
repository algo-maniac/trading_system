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


def test_cycle_catches_unexpected_exception_and_retries_cleanly(bus):
    """A non-auth failure (network/DB/etc.) must not crash the cycle — it should report
    an error status so the next scheduled tick can retry, instead of the whole process
    dying or the failure vanishing silently."""
    def boom(*a, **k):
        raise ConnectionError("kite api unreachable")

    s = _scheduler(bus, ingest_fn=boom)
    now = datetime(2024, 1, 2, 10, 5, tzinfo=IST)
    r = s.run_cycle(now=now)
    assert r["status"] == "error"
    assert "kite api unreachable" in r["error"]
    assert r["published"] == 0
    # next tick (no code change needed) succeeds normally — proves retry-friendliness
    s2 = _scheduler(bus)
    s2._last_published = s._last_published  # same cursor state, simulating the same instance
    r2 = s2.run_cycle(now=now)
    assert r2["status"] == "ok"


def test_day_mode_default_cron_is_frequent_not_single_shot():
    """Guards the retry-friendliness fix: day-mode must poll often (window gate decides
    WHEN to act), not fire once precisely at 15:45 with no second chance."""
    from trading_system.config import get_settings
    cron = get_settings().reader_schedule_cron
    assert cron != "45 15 * * *", (
        "a single once-a-day cron shot has no retry if that one invocation is missed "
        "or fails — see agents/reader/scheduler.py ReaderScheduler.start() docstring"
    )


def test_day_mode_skips_repeat_ingest_after_success_same_day(bus):
    """Once a day-mode cycle succeeds, later same-day ticks must not re-ingest the whole
    universe every 10 minutes for the rest of the evening — wasteful Kite quota burn
    for data that can no longer change post-close."""
    calls = {"n": 0}

    def counting_ingest(*a, **k):
        calls["n"] += 1
        return []

    s = ReaderScheduler(
        bus, TokenManager(alert_bus=bus),
        symbols=["RELIANCE"], intervals=["day"], cron="*/10 * * * *",
        ingest_fn=counting_ingest, read_bars_fn=_fake_bars,
    )
    now = datetime(2024, 1, 2, 16, 0, tzinfo=IST)   # in the day-mode window
    r1 = s.run_cycle(now=now)
    assert r1["status"] == "ok" and calls["n"] == 1
    r2 = s.run_cycle(now=now.replace(minute=10))
    assert r2["status"] == "already_done_today" and calls["n"] == 1  # no second ingest
    # next trading day: resumes normally
    tomorrow = datetime(2024, 1, 3, 16, 0, tzinfo=IST)
    r3 = s.run_cycle(now=tomorrow)
    assert r3["status"] == "ok" and calls["n"] == 2


def test_day_mode_retries_after_a_failed_cycle(bus):
    """A failure must NOT set the success guard — the next tick should retry."""
    attempts = {"n": 0}

    def flaky_ingest(*a, **k):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("transient")
        return []

    s = ReaderScheduler(
        bus, TokenManager(alert_bus=bus),
        symbols=["RELIANCE"], intervals=["day"], cron="*/10 * * * *",
        ingest_fn=flaky_ingest, read_bars_fn=_fake_bars,
    )
    now = datetime(2024, 1, 2, 16, 0, tzinfo=IST)
    r1 = s.run_cycle(now=now)
    assert r1["status"] == "error"
    r2 = s.run_cycle(now=now.replace(minute=10))
    assert r2["status"] == "ok"  # retried and succeeded
