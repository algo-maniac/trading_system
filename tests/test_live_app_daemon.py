"""reader-live daemon loop: it must SLEEP-and-recheck on non-trading days / missing
tokens, never exit (compose restart:unless-stopped would busy-loop an exit). Regression
for the weekend restart loop (150+ restarts)."""
import threading
from datetime import date
from unittest.mock import patch

from trading_system.agents.reader import live_app
from trading_system.data.auth import AuthError


class OneShotStop(threading.Event):
    """A stop event that trips after `after` wait() calls, so the daemon loop runs a
    bounded number of iterations instead of forever."""

    def __init__(self, after: int = 1) -> None:
        super().__init__()
        self._remaining = after
        self.waits: list[float | None] = []

    def wait(self, timeout=None):  # type: ignore[override]
        self.waits.append(timeout)
        self._remaining -= 1
        if self._remaining <= 0:
            self.set()
        return self.is_set()


class _Settings:
    reader_token_poll_seconds = 30.0


def test_non_trading_day_waits_does_not_exit():
    stop = OneShotStop(after=1)
    with patch.object(live_app, "is_trading_day", return_value=False), \
         patch.object(live_app, "get_authenticated_kite") as kite, \
         patch.object(live_app, "_run_session") as run:
        live_app._daemon_loop(_Settings(), stop)
    # It slept on the idle re-check timeout instead of returning/exiting immediately,
    # and never tried to authenticate or open a session on a non-trading day.
    assert stop.waits == [live_app._IDLE_RECHECK_SECONDS]
    kite.assert_not_called()
    run.assert_not_called()


def test_missing_token_alerts_and_waits_does_not_exit():
    stop = OneShotStop(after=1)
    alerts = []
    fake_bus = type("B", (), {"publish_alert": lambda self, k, d: alerts.append(k)})()
    with patch.object(live_app, "is_trading_day", return_value=True), \
         patch.object(live_app, "get_authenticated_kite",
                      side_effect=AuthError("no token today")), \
         patch.object(live_app, "RedisBarBus", return_value=fake_bus), \
         patch.object(live_app, "_run_session") as run:
        live_app._daemon_loop(_Settings(), stop)
    assert alerts == ["token_expired"]
    assert stop.waits == [30.0]          # waited the token-poll interval, didn't exit
    run.assert_not_called()


def test_trading_day_with_token_runs_a_session():
    stop = OneShotStop(after=1)
    with patch.object(live_app, "is_trading_day", return_value=True), \
         patch.object(live_app, "get_authenticated_kite", return_value=object()), \
         patch.object(live_app, "_run_session") as run:
        # _run_session returns immediately (stop already set inside by the loop guard);
        # set stop so the loop ends after one session.
        run.side_effect = lambda *a, **k: stop.set()
        live_app._daemon_loop(_Settings(), stop)
    run.assert_called_once()


def test_session_exception_does_not_kill_daemon():
    stop = OneShotStop(after=2)
    with patch.object(live_app, "is_trading_day", return_value=True), \
         patch.object(live_app, "get_authenticated_kite", return_value=object()), \
         patch.object(live_app, "_run_session", side_effect=RuntimeError("kite blip")):
        live_app._daemon_loop(_Settings(), stop)   # must not raise
    assert stop.waits == [60, 60]         # backed off after each failed session


class _FakeBus:
    def __init__(self) -> None:
        self.alerts: list[str] = []

    def publish_alert(self, kind, detail):  # noqa: ANN001
        self.alerts.append(kind)


def _reader(focus=("RELIANCE",)):
    with patch.object(live_app, "RedisBarBus", return_value=_FakeBus()):
        return live_app.LiveReader(list(focus), {s: i for i, s in enumerate(focus)})


def test_on_reconnect_below_threshold_does_not_request_rebuild():
    reader = _reader()
    # Transient drops that would succeed reset the count in KiteTicker; even a handful
    # of attempts short of the threshold must not tear the ticker down.
    reader.on_reconnect(None, 1)
    reader.on_reconnect(None, live_app._REBUILD_AFTER_RECONNECTS - 1)
    assert not reader.needs_rebuild()


def test_on_reconnect_at_threshold_requests_rebuild():
    reader = _reader()
    reader.on_reconnect(None, live_app._REBUILD_AFTER_RECONNECTS)
    # This is what breaks the 403-forever loop: the supervisor sees it and rebuilds
    # the ticker with a freshly-read token (the daily-rotation recovery).
    assert reader.needs_rebuild()


def test_flapping_triggers_rebuild_even_when_attempts_stay_low():
    # The morning-after case: connect → stream → 403 → reconnect, over and over. Each
    # on_connect resets KiteTicker's `attempts` to 1, so the consecutive-count check never
    # fires — but the rolling reconnect-rate does.
    reader = _reader()
    clock = [1000.0]
    reader._clock = lambda: clock[0]
    for _ in range(live_app._FLAP_MAX_RECONNECTS - 1):
        reader.on_reconnect(None, 1)      # attempts stays low (link keeps briefly reconnecting)
        clock[0] += 20                    # 20s apart → all inside the 180s window
    assert not reader.needs_rebuild()
    reader.on_reconnect(None, 1)          # the Nth reconnect inside the window
    assert reader.needs_rebuild()


def test_reconnects_spread_beyond_window_do_not_trigger_rebuild():
    # Occasional drops far apart (a flaky network, not a broken link) must NOT rebuild.
    reader = _reader()
    clock = [1000.0]
    reader._clock = lambda: clock[0]
    for _ in range(10):
        reader.on_reconnect(None, 1)
        clock[0] += live_app._FLAP_WINDOW_SECONDS + 1   # each reconnect ages out the last
    assert not reader.needs_rebuild()


def test_ist_today_uses_ist_not_utc():
    # 18:56 UTC on a Sunday is already past midnight IST → Monday. date.today() (UTC)
    # would say Sunday; _ist_today must say Monday.
    from datetime import datetime
    from zoneinfo import ZoneInfo
    fixed = datetime(2026, 7, 5, 19, 30, tzinfo=ZoneInfo("UTC"))

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz)

    with patch.object(live_app, "datetime", _DT):
        assert live_app._ist_today() == date(2026, 7, 6)   # Monday IST
