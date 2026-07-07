"""Token-notifier agent entrypoint: wires TokenNotifierCore to the real world.

Every `token_alert_cron` tick (default */20 min) it checks Redis for today's Kite
access token and emails a renewal reminder (via SMTP) while it's missing, inside the
[token_alert_start_ist, token_alert_end_ist] window on trading days — then sends one
confirmation and goes quiet once the token is renewed.

  python -m trading_system.agents.token_notifier.app          # run as a service
  python -m trading_system.agents.token_notifier.app --once   # one check now, exit
"""
import argparse
import signal
import sys
import threading
from datetime import datetime, time
from zoneinfo import ZoneInfo

from trading_system.agents.token_notifier.core import TokenNotifierCore
from trading_system.config import get_settings
from trading_system.data.auth import get_access_token
from trading_system.data.calendar import is_trading_day
from trading_system.net.mailer import send_email
from trading_system.observability import get_logger, setup_logging

logger = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")


def _parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def build_core() -> TokenNotifierCore:
    s = get_settings()
    return TokenNotifierCore(
        has_token=lambda: get_access_token() is not None,
        send=send_email,
        now=lambda: datetime.now(IST),
        is_trading_day=is_trading_day,
        start=_parse_hhmm(s.token_alert_start_ist),
        end=_parse_hhmm(s.token_alert_end_ist),
    )


def run_tick(core: TokenNotifierCore) -> str:
    outcome = core.tick()
    logger.info("token_notifier_tick", outcome=outcome)
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kite token-expiry email notifier.")
    parser.add_argument("--once", action="store_true", help="run one check now and exit")
    args = parser.parse_args(argv)

    s = get_settings()
    setup_logging(s.log_level)
    core = build_core()
    if args.once:
        run_tick(core)
        return 0

    if not (s.smtp_user and s.smtp_password and s.alert_email_to):
        # Don't silently pretend to guard the token — make the misconfig loud, but keep
        # running so simply adding the .env vars + restart is enough to activate it.
        logger.warning("token_notifier_unconfigured",
                       detail="SMTP_USER/SMTP_PASSWORD/ALERT_EMAIL_TO unset — reminders will no-op")

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    sched = BackgroundScheduler(timezone=IST)
    sched.add_job(lambda: run_tick(core),
                  CronTrigger.from_crontab(s.token_alert_cron, timezone=IST),
                  id="token_notifier", max_instances=1, coalesce=True,
                  misfire_grace_time=None)
    sched.start()
    logger.info("token_notifier_started", cron=s.token_alert_cron,
                start=s.token_alert_start_ist, end=s.token_alert_end_ist)

    stop = threading.Event()

    def _handle(signum: int, _frame: object) -> None:
        logger.info("token_notifier_shutdown_signal", signum=signum)
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    run_tick(core)   # startup catch-up, same idiom as reader/analyst
    stop.wait()
    sched.shutdown(wait=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
