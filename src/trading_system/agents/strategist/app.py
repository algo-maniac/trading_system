"""Strategist agent entrypoint: consume live bars, run the validated 3-sleeve book,
publish SignalEvents. Crash-only: no state that matters lives here — position views read
the Trader's ledger, and the bus consumer group resumes from the last un-acked message.

Run as a container:
  python -m trading_system.agents.strategist.app
"""
import signal
import sys

from trading_system.agents.heartbeat import Heartbeat
from trading_system.agents.strategist.runner import Sleeve, StrategistRunner
from trading_system.bus.redis_streams import RedisBarBus, RedisSignalBus
from trading_system.config import get_settings
from trading_system.data.storage import get_connection
from trading_system.execution.ledger import PostgresLedger
from trading_system.observability import get_logger, setup_logging
from trading_system.portfolio.live import LivePortfolio
from trading_system.strategy.registry import get_strategy

logger = get_logger(__name__)

GROUP = "strategist"
CONSUMER = "strategist-1"

# The Phase 5R/5V validated book. Sleeve names must match the Trader's SleeveExecution
# names and settings.paper_sleeve_weights keys.
SLEEVE_SPECS: list[tuple[str, str, dict, str]] = [
    # (sleeve, strategy, params, interval)
    ("momentum", "xsec_momentum", {"top_n": 20, "abs_momentum_min": 0.2}, "day"),
    ("stabilizer", "swing_pullback", {}, "day"),
    ("event_intraday", "orb", {"gap_min": 0.01}, "5minute"),
]


def main() -> int:
    setup_logging(get_settings().log_level)
    # `with` (not get_connection().__enter__()) — the latter drops the context-manager
    # reference immediately, and CPython garbage-collecting it triggers __exit__ on the
    # generator, closing the connection within microseconds ("the connection is closed").
    with get_connection() as conn:
        ledger = PostgresLedger(conn)
        signal_bus = RedisSignalBus()
        bar_bus = RedisBarBus()

        sleeves = [
            Sleeve(name=name, strategy=get_strategy(strat, **params), interval=interval,
                   view=LivePortfolio(ledger, name))
            for name, strat, params, interval in SLEEVE_SPECS
        ]
        runner = StrategistRunner(sleeves, publish=signal_bus.publish)

        stop = {"flag": False}

        def _handle(signum: int, _frame: object) -> None:
            logger.info("strategist_shutdown_signal", signum=signum)
            stop["flag"] = True

        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)

        logger.info("strategist_start", sleeves=[s.name for s in sleeves])
        heartbeat = Heartbeat(interval_seconds=60.0)
        # Replay this consumer's own un-acked messages first (crash recovery), then live.
        for msg_id, bar in bar_bus.read_pending(GROUP, CONSUMER):
            runner.on_bar(bar)
            bar_bus.ack(GROUP, msg_id)
        while not stop["flag"]:
            for msg_id, bar in bar_bus.poll(GROUP, CONSUMER, block_ms=1000):
                runner.on_bar(bar)
                bar_bus.ack(GROUP, msg_id)
            # Day-mode gets exactly one batch/day — without this, a cross-sectional
            # sleeve's signals wait for TOMORROW's batch to trigger the flush, delaying
            # every entry by a full trading day. 90s safely clears the reader's
            # few-second all-symbols publish burst before treating a batch as complete.
            runner.flush_idle(idle_seconds=90.0)
            # Alive-but-silent is the common case (most bars don't trigger a signal) —
            # without this, "quietly working" and "quietly stuck" look identical in logs.
            heartbeat.tick(
                "strategist_heartbeat", bars_processed=runner.bars_processed,
                signals_published=runner.signals_published,
            )
        runner.flush()
        logger.info("strategist_stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
