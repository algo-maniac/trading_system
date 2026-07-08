"""scheduler_watchdog: the pure health decision, plus that the thread force-exits via the
injected action when a job's next_run_time slips past the grace period (host-suspend wedge)."""
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from trading_system.agents.scheduler_watchdog import scheduler_unhealthy, start_watchdog

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 7, 8, 8, 10, tzinfo=IST)


def test_healthy_when_next_run_is_in_the_future():
    nrt = NOW + timedelta(minutes=10)
    assert scheduler_unhealthy(running=True, next_run_time=nrt, now=NOW, grace_seconds=600) is False


def test_healthy_within_grace_even_if_slightly_overdue():
    nrt = NOW - timedelta(seconds=120)     # 2 min late, grace 600 → tolerated
    assert scheduler_unhealthy(running=True, next_run_time=nrt, now=NOW, grace_seconds=600) is False


def test_unhealthy_when_overdue_beyond_grace():
    nrt = NOW - timedelta(seconds=900)     # 15 min late → wedged
    assert scheduler_unhealthy(running=True, next_run_time=nrt, now=NOW, grace_seconds=600) is True


def test_unhealthy_when_not_running():
    assert scheduler_unhealthy(running=False, next_run_time=None, now=NOW, grace_seconds=600) is True


def test_paused_job_with_no_next_run_is_treated_healthy():
    assert scheduler_unhealthy(running=True, next_run_time=None, now=NOW, grace_seconds=600) is False


def test_watchdog_thread_fires_exit_action_when_wedged():
    # A scheduler whose job is far overdue must trigger the (injected) exit action.
    overdue = datetime.now(IST) - timedelta(hours=2)
    sched = SimpleNamespace(running=True,
                            get_job=lambda _id: SimpleNamespace(next_run_time=overdue))
    fired = threading.Event()
    stop = threading.Event()
    start_watchdog(sched, "job", stop, grace_seconds=600.0, check_interval=0.05,
                   on_unhealthy=fired.set)
    assert fired.wait(timeout=2.0)         # watchdog detected the wedge and acted
    stop.set()


def test_watchdog_thread_stays_quiet_when_healthy():
    healthy = datetime.now(IST) + timedelta(minutes=10)
    sched = SimpleNamespace(running=True,
                            get_job=lambda _id: SimpleNamespace(next_run_time=healthy))
    fired = threading.Event()
    stop = threading.Event()
    start_watchdog(sched, "job", stop, grace_seconds=600.0, check_interval=0.05,
                   on_unhealthy=fired.set)
    assert not fired.wait(timeout=0.5)     # never triggers on a healthy scheduler
    stop.set()
