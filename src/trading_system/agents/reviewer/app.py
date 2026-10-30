"""Reviewer agent entrypoint: consumes trade.signals in its own consumer group and
journals a shadow LLM verdict per BUY signal. The trader is unaffected either way.
Crash-only: verdicts are keyed by signal id, un-acked signals are replayed on start.

Run as a container:  python -m trading_system.agents.reviewer.app
"""
import signal
import sys

from trading_system.agents.heartbeat import Heartbeat
from trading_system.agents.reviewer import repo
from trading_system.agents.reviewer.core import ReviewerCore, ReviewerDeps
from trading_system.bus.redis_streams import RedisBarBus, RedisSignalBus
from trading_system.config import get_settings
from trading_system.data.storage import get_connection
from trading_system.llm.signal_review import AnthropicSignalReviewer, MockSignalReviewer
from trading_system.net.limits import DailyBudget, MonthlySpend
from trading_system.observability import get_logger, setup_logging

logger = get_logger(__name__)

GROUP = "reviewer"
CONSUMER = "reviewer-1"


def build_reviewer():  # type: ignore[no-untyped-def]
    s = get_settings()
    if s.llm_provider != "anthropic":
        return MockSignalReviewer()
    import anthropic
    return AnthropicSignalReviewer(
        client=anthropic.Anthropic(api_key=s.anthropic_api_key),
        call_budget=DailyBudget("signal_review", s.signal_review_daily_budget),
        # Same "llm" key as the admission reviewer: one monthly cap across both agents.
        spend=MonthlySpend("llm", s.llm_monthly_usd_cap, alert_usd=s.llm_spend_alert_usd),
        alert=RedisBarBus().publish_alert,
        model=s.signal_review_model,
        max_turns=s.signal_review_max_turns,
    )


def main() -> int:
    s = get_settings()
    setup_logging(s.log_level)
    reviewer = build_reviewer()
    with get_connection() as conn:
        conn.autocommit = True  # each verdict is its own durable write
        core = ReviewerCore(ReviewerDeps(
            review=reviewer.review,
            tools_for=lambda sig: repo.PostgresReviewTools(conn, as_of=sig.ts),
            already_reviewed=lambda sid: repo.has_review(conn, sid),
            save=lambda sig, v: repo.save_review(conn, sig, v, reviewer.name),
        ))
        bus = RedisSignalBus()
        stop = {"flag": False}

        def _handle(signum: int, _frame: object) -> None:
            logger.info("reviewer_shutdown_signal", signum=signum)
            stop["flag"] = True

        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)
        logger.info("reviewer_start", reviewer=reviewer.name)

        for msg_id, sig in bus.read_pending(GROUP, CONSUMER):
            core.on_signal(sig)
            bus.ack(GROUP, msg_id)
        heartbeat = Heartbeat(interval_seconds=60.0)
        while not stop["flag"]:
            for msg_id, sig in bus.poll(GROUP, CONSUMER, block_ms=1000):
                core.on_signal(sig)
                bus.ack(GROUP, msg_id)  # ack after the verdict is journaled
            heartbeat.tick("reviewer_heartbeat", reviewed=core.reviewed, skipped=core.skipped)
        logger.info("reviewer_stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
