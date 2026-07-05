"""Replayer agent entrypoint (replay namespace only).

Re-publishes stored historic bars onto the bus at accelerated pace so the normal
Strategist → Trader → Reporter stack paper-trades the past. Run it inside the
replay compose project (DATABASE_URL=trading_replay, REDIS_URL=redis db 1) AFTER
the consumers are up — fresh consumer groups start at the stream tail, so bars
published before the group exists would be invisible to them.

  docker compose -f docker-compose.replay.yml run --rm replayer \
      --from 2025-01-01 --to 2025-04-03 --intervals day

--with-news adds the Analyst step each morning (StockInsights date-window fetch,
MockLLM by default; admissions judge on already-stored history — no Kite calls).
"""
import argparse
import sys
import time as _time
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from trading_system.agents.heartbeat import Heartbeat
from trading_system.agents.replayer.core import ReplayerCore, ReplayPlan
from trading_system.bus.redis_streams import RedisBarBus
from trading_system.config import get_settings
from trading_system.data.calendar import get_trading_days
from trading_system.data.news_store import active_universe_members
from trading_system.data.screener import symbol_quant_stats, volatile_universe
from trading_system.data.storage import get_connection, get_symbol_token_map
from trading_system.execution.ledger import PostgresLedger
from trading_system.observability import get_logger, setup_logging
from trading_system.replay.engine import ReplayEngine

logger = get_logger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
_SWEEP_TIME = time(8, 15)   # virtual morning-sweep clock, mirrors the live cron
_FOCUS_CAP = 50


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _day_universe(conn) -> list[str]:  # type: ignore[no-untyped-def]
    """Same union as the live EOD reader: N200 ∪ admitted ∪ held."""
    s = get_settings()
    admitted = active_universe_members(conn)
    held = sorted({p.symbol for p in PostgresLedger(conn).open_positions()})
    return list(dict.fromkeys(list(s.nifty_200_symbols) + admitted + held))


def _intraday_focus(conn, day: date) -> list[str]:  # type: ignore[no-untyped-def]
    """Mirror of reader-live's fallback focus: volatile screen ∪ liquid ∪ held."""
    s = get_settings()
    held = sorted({p.symbol for p in PostgresLedger(conn).open_positions()})
    focus = list(dict.fromkeys(
        held + volatile_universe(conn, s.nifty_200_symbols, day) + list(s.liquid_symbols)
    ))
    return focus[:_FOCUS_CAP]


def _make_analyst_hook(conn):  # type: ignore[no-untyped-def]
    """Analyst step with a VIRTUAL clock: run_cycle already takes `now`; the two
    deps that internally use date.today() (quant stats, volatile screen) are rebound
    per simulated day so admission decisions don't peek at post-replay data."""
    from trading_system.agents.analyst.app import build_core, run_cycle

    s = get_settings()
    core = build_core(conn)

    def hook(day: date) -> None:
        core.d.quant_stats = lambda sym: (
            None if (row := symbol_quant_stats(conn, sym, day)) is None
            else {"median_turnover": row.median_turnover, "sessions": row.sessions,
                  "avg_range_pct": row.avg_range_pct}
        )
        core.d.volatile_screen = lambda: volatile_universe(conn, s.nifty_200_symbols, day)
        run_cycle(core, now=datetime.combine(day, _SWEEP_TIME, tzinfo=_IST))

    return hook


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Accelerated historic replay conductor.")
    parser.add_argument("--from", dest="from_date", required=True, type=_parse_date)
    parser.add_argument("--to", dest="to_date", required=True, type=_parse_date)
    parser.add_argument("--intervals", default="day",
                        help="comma list: day | day,5minute")
    parser.add_argument("--day-delay", type=float, default=None,
                        help="seconds between sessions (default from settings)")
    parser.add_argument("--with-news", action="store_true",
                        help="run the Analyst sweep each simulated morning")
    args = parser.parse_args(argv)

    s = get_settings()
    setup_logging(s.log_level)
    intervals = tuple(i.strip() for i in args.intervals.split(",") if i.strip())
    bad = set(intervals) - {"day", "5minute"}
    if bad:
        parser.error(f"unsupported intervals: {sorted(bad)}")
    if s.namespace != "replay":
        # Guard rail: publishing historic bars into the LIVE bus would feed the live
        # paper book years-old prices. Refuse unless explicitly namespaced.
        logger.error("replayer_refused_namespace", namespace=s.namespace)
        print("refusing to run: NAMESPACE must be 'replay' (use docker-compose.replay.yml)",
              file=sys.stderr)
        return 2

    plan = ReplayPlan(
        from_date=args.from_date, to_date=args.to_date, intervals=intervals,
        day_delay=args.day_delay if args.day_delay is not None
        else s.replay_day_delay_seconds,
        intraday_delay=s.replay_intraday_delay_seconds,
        with_news=args.with_news,
    )

    bus = RedisBarBus()
    with get_connection() as conn:
        # Before ANY query: PostgresLedger sets autocommit=True on this connection,
        # and psycopg refuses that once a plain SELECT has opened a transaction
        # (ProgrammingError: connection in INTRANS) — caught on the first live run.
        conn.autocommit = True
        def load_bars(interval: str, day: date):  # type: ignore[no-untyped-def]
            if interval == "day":
                symbols, version = _day_universe(conn), "v3"
            else:
                symbols, version = _intraday_focus(conn, day), "intraday_v2"
            token_map = get_symbol_token_map(conn, symbols)
            return ReplayEngine(symbols, token_map, interval, day, day, version).run()

        core = ReplayerCore(
            publish=bus.publish,
            load_bars=load_bars,
            trading_days=get_trading_days,
            sleep=_time.sleep,
            analyst_hook=_make_analyst_hook(conn) if args.with_news else None,
            alert=bus.publish_alert,
            heartbeat=Heartbeat(interval_seconds=30.0),
        )
        core.run(plan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
