"""Reporter agent entrypoint: poll the ledger, generate due reports, write DB + files.

Data-driven (never wall-clock): a period is generated the moment the ledger shows a
later period has begun — identical behavior live (next morning) and in replay
(seconds later). Files land at {reports_dir}/{namespace}/{period_type}/{start}.md.

Run as a container:  python -m trading_system.agents.reporter.app
"""
import argparse
import signal
import sys
import threading
from pathlib import Path

from trading_system.agents.heartbeat import Heartbeat
from trading_system.agents.reporter import repo
from trading_system.agents.reporter.core import Period, ReporterCore, ReporterDeps
from trading_system.bus.redis_streams import RedisBarBus
from trading_system.config import get_settings
from trading_system.data.storage import get_connection
from trading_system.observability import get_logger, setup_logging

logger = get_logger(__name__)


def build_core(conn) -> ReporterCore:  # type: ignore[no-untyped-def]
    s = get_settings()
    deps = ReporterDeps(
        equity_rows=lambda f, t: repo.equity_rows(conn, f, t),
        closed_positions=lambda f, t: repo.closed_positions(conn, f, t),
        total_fees=lambda f, t: repo.total_fees(conn, f, t),
        open_positions=lambda: repo.open_positions_now(conn),
        signal_count=lambda f, t: repo.signal_count(conn, f, t),
        order_count=lambda f, t: repo.order_count(conn, f, t),
        fill_count=lambda f, t: repo.fill_count(conn, f, t),
        admissions=lambda f, t: repo.admissions_between(conn, f, t),
        news_count=lambda f, t: repo.news_count(conn, f, t),
        budget_snapshot=repo.budget_snapshot,
    )
    return ReporterCore(deps, namespace=s.namespace)


def write_report_file(markdown: str, period: Period) -> Path:
    s = get_settings()
    path = (Path(s.reports_dir) / s.namespace / period.period_type
            / f"{period.start.isoformat()}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown)
    return path


def generate_pending(conn, core: ReporterCore,  # type: ignore[no-untyped-def]
                     alert=None) -> int:
    """One pass: find due periods, generate, persist, write files. Returns #written."""
    from trading_system.agents.reporter.core import pending_periods
    due = pending_periods(repo.snapshot_dates(conn), repo.reported_periods(conn))
    written = 0
    for period in due:
        try:
            payload, markdown = core.build_report(period)
            if repo.insert_report(conn, period, payload, markdown):
                path = write_report_file(markdown, period)
                logger.info("report_written", period=period.period_type,
                            start=str(period.start), path=str(path))
                if alert is not None:
                    alert("report_ready", f"{period.period_type} {period.start}")
                written += 1
        except Exception as exc:
            # One bad period must not block the rest (or crash the agent).
            logger.error("report_failed", period=period.period_type,
                         start=str(period.start), error=str(exc), exc_info=True)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reporter agent (data-driven periods).")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    args = parser.parse_args(argv)

    s = get_settings()
    setup_logging(s.log_level)
    with get_connection() as conn:
        core = build_core(conn)
        bus = RedisBarBus()
        if args.once:
            n = generate_pending(conn, core, alert=bus.publish_alert)
            logger.info("reporter_once_done", written=n)
            return 0

        stop = threading.Event()

        def _handle(signum: int, _frame: object) -> None:
            logger.info("reporter_shutdown_signal", signum=signum)
            stop.set()

        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)

        logger.info("reporter_started", namespace=s.namespace,
                    poll_seconds=s.reporter_poll_seconds)
        heartbeat = Heartbeat(interval_seconds=300.0)
        total = 0
        while not stop.is_set():
            total += generate_pending(conn, core, alert=bus.publish_alert)
            heartbeat.tick("reporter_heartbeat", reports_written=total)
            stop.wait(timeout=s.reporter_poll_seconds)
        logger.info("reporter_stopped", reports_written=total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
