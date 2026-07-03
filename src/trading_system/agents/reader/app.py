"""Reader agent entrypoint.

A long-running container that ingests the liquid universe on a schedule and publishes new bars
to the bus, self-healing on token expiry (alert + pause + auto-resume).

Run as a container:  python -m trading_system.agents.reader.app
On-demand historical backfills still use the existing CLI: `trading ingest ...`.
"""
import signal
import sys
import threading

from trading_system.agents.reader.scheduler import ReaderScheduler
from trading_system.agents.reader.token_manager import TokenManager
from trading_system.bus.redis_streams import RedisBarBus
from trading_system.config import get_settings
from trading_system.observability import get_logger, setup_logging

logger = get_logger(__name__)


def _day_universe() -> list[str]:
    """Swing universe, resolved fresh per cycle: Nifty 200 ∪ news-admitted members ∪
    held positions (an exit must always have a data feed)."""
    from trading_system.data.news_store import active_universe_members
    from trading_system.data.storage import get_connection
    from trading_system.execution.ledger import PostgresLedger

    s = get_settings()
    with get_connection() as conn:
        admitted = active_universe_members(conn)
        held = sorted({p.symbol for p in PostgresLedger(conn).open_positions()})
    return list(dict.fromkeys(list(s.nifty_200_symbols) + admitted + held))


def build_scheduler() -> ReaderScheduler:
    s = get_settings()
    bus = RedisBarBus()
    token_manager = TokenManager(alert_bus=bus)
    day_mode = s.reader_intervals == ["day"]
    return ReaderScheduler(
        bus, token_manager,
        # Day mode = the swing sleeves' EOD path: dynamic universe (N200 ∪ admitted ∪
        # held) with v3 features attached. Intraday-interval mode keeps the legacy
        # liquid subset (superseded by live_app's WebSocket plane).
        symbols=s.nifty_200_symbols if day_mode else s.liquid_symbols,
        intervals=s.reader_intervals,
        cron=s.reader_schedule_cron,
        workers=s.ingest_workers,
        feature_version="v3" if day_mode else None,
        symbols_fn=_day_universe if day_mode else None,
    )


def main(argv: list[str] | None = None) -> int:
    setup_logging(get_settings().log_level)
    scheduler = build_scheduler()

    stop = threading.Event()

    def _handle(signum: int, _frame: object) -> None:
        logger.info("reader_shutdown_signal", signum=signum)
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    logger.info("reader_start", symbols=len(scheduler.symbols), intervals=scheduler.intervals)
    # Startup catch-up so we don't wait up to an hour for the first pull.
    scheduler.run_cycle()
    scheduler.start()
    try:
        stop.wait()  # block until a shutdown signal
    finally:
        scheduler.shutdown()
        logger.info("reader_stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
