"""Trader agent entrypoint: the execution owner. Consumes SignalEvents (gate → paper
orders) and bars (fills + position protection + price marks); snapshots equity EOD.
Crash-only: all state is in Postgres; restart resumes from consumer-group offsets.

Run as a container:
  python -m trading_system.agents.trader.app
"""
import signal
import sys
from datetime import UTC, datetime, time

from trading_system.agents.trader.core import SleeveExecution, TraderCore
from trading_system.bus.redis_streams import RedisBarBus, RedisSignalBus
from trading_system.config import get_settings
from trading_system.data.storage import get_connection
from trading_system.execution.fees import Product
from trading_system.execution.ledger import PostgresLedger
from trading_system.execution.paper_broker import PaperBroker
from trading_system.observability import get_logger, setup_logging
from trading_system.risk.models import RiskConfig

logger = get_logger(__name__)

GROUP = "trader"
CONSUMER = "trader-1"
_EOD_SNAPSHOT = time(15, 35)  # after close+square-off; once per day

# Per-sleeve execution config. Risk limits sized for each sleeve's design:
#   momentum   — xsec holds top_n=20 names at ~5% each → caps must not bind below that
#   stabilizer — swing_pullback's validated defaults match RiskConfig defaults
#   event_intraday — MIS product, up to ~5 concurrent gap trades at 20% size_hint
SLEEVES = [
    SleeveExecution("momentum", Product.CNC,
                    RiskConfig(max_position_pct=0.10, max_positions=25)),
    SleeveExecution("stabilizer", Product.CNC, RiskConfig()),
    SleeveExecution("event_intraday", Product.MIS,
                    RiskConfig(max_position_pct=0.25, max_positions=5)),
]


def main() -> int:
    s = get_settings()
    setup_logging(s.log_level)
    conn = get_connection().__enter__()
    ledger = PostgresLedger(conn)
    broker = PaperBroker(ledger, slippage_bps=s.paper_slippage_bps, fees_enabled=True)
    core = TraderCore(ledger, broker, SLEEVES, s.paper_capital, s.paper_sleeve_weights)

    bar_bus = RedisBarBus()
    signal_bus = RedisSignalBus()

    stop = {"flag": False}

    def _handle(signum: int, _frame: object) -> None:
        logger.info("trader_shutdown_signal", signum=signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    logger.info("trader_start", sleeves=[sl.name for sl in SLEEVES])
    # Crash recovery: drain this consumer's un-acked messages first.
    for msg_id, sig in signal_bus.read_pending(GROUP, CONSUMER):
        core.on_signal(sig)
        signal_bus.ack(GROUP, msg_id)
    for msg_id, bar in bar_bus.read_pending(GROUP, CONSUMER):
        core.on_bar(bar)
        bar_bus.ack(GROUP, msg_id)

    last_snapshot_day = None
    while not stop["flag"]:
        for msg_id, sig in signal_bus.poll(GROUP, CONSUMER, block_ms=200):
            core.on_signal(sig)
            signal_bus.ack(GROUP, msg_id)
        for msg_id, bar in bar_bus.poll(GROUP, CONSUMER, block_ms=200):
            core.on_bar(bar)
            bar_bus.ack(GROUP, msg_id)
        now = datetime.now(UTC)
        # IST session close ≈ 10:05 UTC; snapshot once after 10:05 UTC each weekday.
        if now.time() >= time(10, 5) and last_snapshot_day != now.date():
            core.snapshot(now)
            last_snapshot_day = now.date()
    core.snapshot(datetime.now(UTC))
    logger.info("trader_stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
