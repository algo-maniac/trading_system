"""Analyst agent entrypoint: wires AnalystCore's injectable deps to the real world.

Schedule (settings.analyst_sweep_cron, default 08:15 / 18:15 / 21:15 IST): each tick
runs sweep → admissions → (morning tick only) focus-list build. `--once` runs a single
full cycle immediately and exits — the manual/verification path.

Run as a container:  python -m trading_system.agents.analyst.app
"""
import argparse
import signal
import sys
import threading
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import redis

from trading_system.agents.analyst.core import AnalystCore, AnalystDeps
from trading_system.bus.redis_streams import RedisBarBus
from trading_system.config import get_settings
from trading_system.data import news_store
from trading_system.data.ingestion import ingest_universe
from trading_system.data.screener import symbol_quant_stats, total_day_sessions, volatile_universe
from trading_system.data.stockinsights_client import StockInsightsClient
from trading_system.data.storage import get_connection
from trading_system.execution.ledger import PostgresLedger
from trading_system.llm import get_llm
from trading_system.observability import get_logger, setup_logging

logger = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")
_FOCUS_BUILD_BEFORE = time(12, 0)  # a sweep before noon also (re)builds today's focus

_REJECTED_KEY = "analyst:rejected:{date}"
_REJECTED_TTL = 3 * 86_400  # self-expiring, matches net.limits' daily-key convention


def _redis() -> redis.Redis:  # type: ignore[type-arg]
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


def rejected_today(today: date) -> set:
    """Symbols already rejected by an earlier sweep today — Redis, not in-process
    state, so it survives an analyst restart same as the admissions themselves do."""
    return set(_redis().smembers(_REJECTED_KEY.format(date=today.isoformat())))


def mark_rejected(symbol: str, today: date) -> None:
    key = _REJECTED_KEY.format(date=today.isoformat())
    r = _redis()
    r.sadd(key, symbol)
    r.expire(key, _REJECTED_TTL)


def build_core(conn) -> AnalystCore:
    s = get_settings()
    if s.replay_mode:
        # Replay namespace: smaller news budget (the PHYSICAL StockInsights quota is
        # shared with live — only the Redis counter is namespaced) and no Kite
        # backfill mid-replay (candidates are judged on already-stored history).
        from trading_system.net.limits import DailyBudget
        client = StockInsightsClient(
            budget=DailyBudget("stockinsights", s.replay_news_budget))
    else:
        client = StockInsightsClient()
    ledger = PostgresLedger(conn)
    bus = RedisBarBus()

    def known_nse_symbols(symbols: list[str]) -> set:
        with conn.cursor() as cur:
            cur.execute("SELECT tradingsymbol FROM instruments WHERE tradingsymbol = ANY(%s)",
                        (symbols,))
            return {r[0] for r in cur.fetchall()}

    def backfill(symbols: list[str]) -> None:
        if s.replay_mode:
            logger.info("admission_backfill_skipped_replay", symbols=symbols)
            return
        two_years_ago = date.today() - timedelta(days=730)
        logger.info("admission_backfill", symbols=symbols)
        ingest_universe(symbols, ["day"], two_years_ago, date.today(), workers=2)

    deps = AnalystDeps(
        fetch_news=client.fetch_announcements,
        store_news=lambda items: news_store.insert_news_items(conn, items),
        news_id_lookup=lambda h: news_store.news_id_for_hash(conn, h),
        upsert_gap_watch=lambda d, sym, direction, nid:
            news_store.upsert_gap_watch(conn, d, sym, direction, nid),
        known_nse_symbols=known_nse_symbols,
        backfill_history=backfill,
        quant_stats=lambda sym: (
            None if (row := symbol_quant_stats(conn, sym, date.today())) is None
            else {"median_turnover": row.median_turnover, "sessions": row.sessions,
                  "avg_range_pct": row.avg_range_pct}
        ),
        total_sessions=lambda sym: total_day_sessions(conn, sym),
        llm=get_llm(),
        admit_member=lambda sym, prov: news_store.admit_universe_member(conn, sym, prov),
        base_universe=lambda: list(s.nifty_200_symbols),
        active_members=lambda: news_store.active_universe_members(conn),
        volatile_screen=lambda: volatile_universe(conn, s.nifty_200_symbols, date.today()),
        liquid_symbols=lambda: list(s.liquid_symbols),
        held_symbols=lambda: sorted({p.symbol for p in ledger.open_positions()}),
        write_focus=lambda d, entries: news_store.write_focus_list(conn, d, entries),
        alert=bus.publish_alert,
        admission_cap_per_day=s.admission_max_new_per_day,
        admissions_today_count=lambda d: news_store.count_admissions_on(conn, d),
        rejected_today=rejected_today,
        mark_rejected=mark_rejected,
    )
    return AnalystCore(deps)


def run_cycle(core: AnalystCore, now: datetime | None = None) -> dict:  # type: ignore[type-arg]
    now = now or datetime.now(IST)
    result = core.run_sweep(now)
    items = result.pop("items", [])
    admitted = core.consider_admissions(items, now.date()) if items else []
    focus_n = 0
    if now.time() <= _FOCUS_BUILD_BEFORE:
        news_syms = sorted({i.nse_symbol for i in items if i.nse_symbol})
        with get_connection() as c2, c2.cursor() as cur:
            cur.execute("SELECT symbol FROM gap_watchlist WHERE watch_date=%s",
                        (now.date(),))
            gap_syms = [r[0] for r in cur.fetchall()]
        focus_n = len(core.build_focus(now.date(), news_syms, gap_syms))
    logger.info("analyst_cycle_done", **result, admitted=len(admitted), focus=focus_n)
    return {**result, "admitted": admitted, "focus": focus_n}


def run_cycle_fresh() -> dict:  # type: ignore[type-arg]
    """One full cycle on a FRESH DB connection, built and torn down per invocation.

    The analyst used to bind ONE long-lived connection at startup; when that connection
    dropped (DB restart / idle timeout) every scheduled cycle failed on the dead handle
    while the process stayed alive — so compose's `restart: unless-stopped` never fired
    and the agent silently wedged (seen live 2026-07-05). A per-cycle connection makes a
    bad connection cost exactly one cycle: the next tick reconnects and self-heals.
    Rebuilding the core each time is safe because the admissions cap is DB-derived, not
    in-process state. Errors are caught + logged loudly (never silent) and the cycle is
    skipped, so one failure can neither crash the agent nor vanish unnoticed."""
    try:
        with get_connection() as conn:
            core = build_core(conn)
            return run_cycle(core)
    except Exception as exc:
        logger.error("analyst_cycle_failed", error=str(exc), exc_info=True)
        return {"status": "error", "error": str(exc)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyst agent (news → universe → focus).")
    parser.add_argument("--once", action="store_true", help="run one cycle now and exit")
    args = parser.parse_args(argv)

    s = get_settings()
    setup_logging(s.log_level)
    if args.once:
        run_cycle_fresh()
        return 0

    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    from trading_system.agents.scheduler_watchdog import start_watchdog

    def _on_scheduler_event(event: object) -> None:
        # Surface job errors/misses through our logger — a silently missed sweep is
        # exactly what hid the original wedge.
        if getattr(event, "exception", None) is not None:
            logger.error("analyst_scheduler_job_error", exception=str(event.exception))
        else:
            logger.warning("analyst_scheduler_job_missed", scheduled_run_time=str(
                getattr(event, "scheduled_run_time", None)))

    sched = BackgroundScheduler(timezone=IST)
    sched.add_job(run_cycle_fresh,
                  CronTrigger.from_crontab(s.analyst_sweep_cron, timezone=IST),
                  id="analyst_cycle", max_instances=1, coalesce=True,
                  misfire_grace_time=None)
    sched.add_listener(_on_scheduler_event, EVENT_JOB_ERROR | EVENT_JOB_MISSED)
    sched.start()
    logger.info("analyst_started", cron=s.analyst_sweep_cron, llm=get_llm().name)

    stop = threading.Event()

    def _handle(signum: int, _frame: object) -> None:
        logger.info("analyst_shutdown_signal", signum=signum)
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    # Restart the process if the scheduler thread dies (e.g. host suspend) so a missed
    # sweep can't go unnoticed; startup catch-up below re-runs the missed cycle.
    start_watchdog(sched, "analyst_cycle", stop, grace_seconds=600.0)
    run_cycle_fresh()   # startup catch-up, same idiom as the reader
    stop.wait()
    sched.shutdown(wait=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
