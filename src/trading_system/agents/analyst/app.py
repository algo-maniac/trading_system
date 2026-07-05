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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyst agent (news → universe → focus).")
    parser.add_argument("--once", action="store_true", help="run one cycle now and exit")
    args = parser.parse_args(argv)

    s = get_settings()
    setup_logging(s.log_level)
    with get_connection() as conn:
        core = build_core(conn)
        if args.once:
            run_cycle(core)
            return 0

        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        sched = BackgroundScheduler(timezone=IST)
        sched.add_job(lambda: run_cycle(core),
                      CronTrigger.from_crontab(s.analyst_sweep_cron, timezone=IST),
                      id="analyst_cycle", max_instances=1, coalesce=True,
                      misfire_grace_time=None)
        sched.start()
        logger.info("analyst_started", cron=s.analyst_sweep_cron, llm=core.d.llm.name)

        stop = threading.Event()

        def _handle(signum: int, _frame: object) -> None:
            logger.info("analyst_shutdown_signal", signum=signum)
            stop.set()

        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)
        run_cycle(core)   # startup catch-up, same idiom as the reader
        stop.wait()
        sched.shutdown(wait=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
