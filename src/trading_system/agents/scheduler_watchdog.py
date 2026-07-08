"""Watchdog that restarts the process when an APScheduler job stops advancing.

APScheduler's BackgroundScheduler runs its loop on a worker thread that can stop firing
after a host suspend/resume — the whole process freezes on sleep and the scheduler's
wake-up math doesn't always recover on resume (seen live 2026-07-08: the analyst missed
its 08:15 sweep and the token-notifier stopped emailing after the Mac slept; the reader,
which fires every 10 min, kept going).

A healthy scheduler always keeps its job's `next_run_time` in the future (it advances the
moment a job is submitted). A wedged one leaves `next_run_time` stuck in the past. This
watchdog polls that on its OWN simple thread (a plain `stop.wait(timeout)` loop, which
resumes cleanly after sleep) and, when the job is overdue beyond a grace period — or the
scheduler reports not-running — force-exits so compose's `restart: unless-stopped` brings
up a fresh process. The agents are crash-only (state in Postgres/Redis, startup catch-up
re-runs the missed cycle), so a hard restart is safe.
"""
import os
import threading
from datetime import datetime

from trading_system.observability import get_logger

logger = get_logger(__name__)


def scheduler_unhealthy(*, running: bool, next_run_time, now, grace_seconds: float) -> bool:
    """Pure decision: is the scheduler wedged? (kept side-effect-free for testing)

    - not running                     → unhealthy
    - no next_run_time (job paused)    → healthy (nothing scheduled to slip)
    - next_run_time older than grace   → unhealthy (a fire was missed and never caught up)
    """
    if not running:
        return True
    if next_run_time is None:
        return False
    return (now - next_run_time).total_seconds() > grace_seconds


def start_watchdog(scheduler, job_id: str, stop: threading.Event, *,
                   grace_seconds: float = 600.0, check_interval: float = 60.0,
                   on_unhealthy=None) -> threading.Thread:
    """Spawn a daemon thread that force-exits when `job_id` stops advancing.

    grace_seconds: how far past a scheduled fire time to tolerate before restarting.
    on_unhealthy: override the action (defaults to os._exit(1)); injected in tests.
    """
    exit_action = on_unhealthy or (lambda: os._exit(1))

    def _run() -> None:
        while not stop.wait(timeout=check_interval):
            try:
                job = scheduler.get_job(job_id)
            except Exception as exc:                      # scheduler mid-shutdown, etc.
                logger.warning("scheduler_watchdog_probe_failed", job_id=job_id, error=str(exc))
                continue
            running = bool(getattr(scheduler, "running", False))
            nrt = getattr(job, "next_run_time", None)
            now = datetime.now(nrt.tzinfo) if nrt is not None else None
            if scheduler_unhealthy(running=running, next_run_time=nrt, now=now,
                                   grace_seconds=grace_seconds):
                logger.error("scheduler_watchdog_restarting", job_id=job_id,
                             running=running, next_run_time=str(nrt))
                exit_action()
                return

    t = threading.Thread(target=_run, name=f"watchdog-{job_id}", daemon=True)
    t.start()
    logger.info("scheduler_watchdog_started", job_id=job_id, grace_seconds=grace_seconds)
    return t
