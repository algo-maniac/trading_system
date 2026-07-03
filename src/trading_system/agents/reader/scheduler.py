"""Scheduled incremental ingestion + bus publish for the Reader agent.

Each cycle (hourly, gated to NSE trading days + session hours):
  1. ingest today's candles for the universe × intervals (idempotent upsert — overlap is safe);
  2. read the day's bars back from the DB and publish only the *new* ones to the bus
     (a per-(symbol, interval) cursor avoids re-publishing bars seen in a previous cycle).

Token errors during ingest are caught by the TokenManager → a `token_expired` alert is raised
and the cycle is skipped (paused); the next cycle resumes automatically once re-authed.

`run_cycle` takes injectable `ingest_fn` / `read_bars_fn` so it is unit-testable without Kite
or a live DB.
"""
from collections.abc import Callable
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from trading_system.agents.reader.token_manager import TokenManager
from trading_system.data.calendar import is_trading_day
from trading_system.data.ingestion import ingest_universe
from trading_system.data.storage import get_connection, get_symbol_token_map, read_ohlcv
from trading_system.observability import get_logger
from trading_system.replay.events import BarEvent

logger = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")
_SESSION_OPEN = time(9, 0)    # coarse gate (real session 09:15); allows a :05 pull buffer
_SESSION_CLOSE = time(15, 40)  # a little past the 15:30 close to catch the final bars
# Day-interval mode publishes ONLY after the close: Kite returns a running (partial)
# day candle during the session, and publishing it would fire swing signals on
# incomplete data. The gate opens after square-off/close and stays open into the
# evening so a late `trading auth` still lets the cycle run.
_DAY_MODE_OPEN = time(15, 45)
_DAY_MODE_CLOSE = time(23, 0)


def read_bars_for_window(
    symbols: list[str], intervals: list[str], from_date: date, to_date: date,
    feature_version: str | None = None,
) -> list[BarEvent]:
    """Read OHLCV bars for the window from the DB as BarEvents.

    With `feature_version` set, bars come via ReplayEngine and CARRY that feature set
    (required for the strategist's swing sleeves — sma/rsi gates never fire on empty
    features). Without it, raw OHLCV with empty features (legacy behavior).
    Sorted by (ts, symbol) to match the engine's expected ordering."""
    if feature_version is not None:
        from trading_system.replay.engine import ReplayEngine
        with get_connection() as conn:
            token_map = get_symbol_token_map(conn, symbols)
        bars = []
        for interval in intervals:
            bars.extend(ReplayEngine(
                symbols, token_map, interval, from_date, to_date, feature_version
            ).run())
        bars.sort()
        return bars
    bars = []
    with get_connection() as conn:
        token_map = get_symbol_token_map(conn, symbols)
        for symbol in symbols:
            token = token_map.get(symbol)
            if not token:
                continue
            for interval in intervals:
                df = read_ohlcv(conn, token, interval, from_date, to_date)
                for row in df.itertuples(index=False):
                    ts = row.ts.to_pydatetime() if hasattr(row.ts, "to_pydatetime") else row.ts
                    bars.append(BarEvent(
                        ts=ts, symbol=symbol, interval=interval, instrument_token=int(token),
                        open=float(row.open), high=float(row.high), low=float(row.low),
                        close=float(row.close), volume=int(row.volume), features={},
                    ))
    bars.sort()
    return bars


class ReaderScheduler:
    def __init__(
        self,
        bus: object,
        token_manager: TokenManager,
        *,
        symbols: list[str],
        intervals: list[str],
        cron: str,
        workers: int = 5,
        feature_version: str | None = None,
        ingest_fn: Callable[..., list] = ingest_universe,
        read_bars_fn: Callable[..., list[BarEvent]] = read_bars_for_window,
    ) -> None:
        self.bus = bus
        self.token_manager = token_manager
        self.symbols = symbols
        self.intervals = intervals
        self.cron = cron
        self.workers = workers
        self.feature_version = feature_version
        self._ingest_fn = ingest_fn
        self._read_bars_fn = read_bars_fn
        self._last_published: dict[tuple[str, str], datetime] = {}
        self._scheduler: object | None = None
        # Day-interval mode: only publish after the close (partial-candle guard).
        if intervals == ["day"]:
            self._window = (_DAY_MODE_OPEN, _DAY_MODE_CLOSE)
        else:
            self._window = (_SESSION_OPEN, _SESSION_CLOSE)

    def _in_session(self, now: datetime) -> bool:
        return self._window[0] <= now.time() <= self._window[1]

    def run_cycle(self, now: datetime | None = None) -> dict:
        """One ingest+publish cycle. Returns a small status dict (also used by tests)."""
        now = now or datetime.now(IST)
        if not is_trading_day(now.date()):
            logger.info("reader_skip_non_trading_day", date=str(now.date()))
            return {"status": "skipped_non_trading_day", "published": 0}
        if not self._in_session(now):
            logger.info("reader_skip_off_hours", at=now.strftime("%H:%M"))
            return {"status": "skipped_off_hours", "published": 0}

        day = now.date()
        # ingest_universe raises AuthError at its top if today's token is missing → guard catches.
        ingested = self.token_manager.guard(
            lambda: self._ingest_fn(self.symbols, self.intervals, day, day, workers=self.workers)
        )
        if ingested is None:
            return {"status": "paused_no_token", "published": 0}

        if self.feature_version is not None:
            bars = self._read_bars_fn(self.symbols, self.intervals, day, day,
                                      feature_version=self.feature_version)
        else:  # legacy call shape — keeps injected test fakes source-compatible
            bars = self._read_bars_fn(self.symbols, self.intervals, day, day)
        published = self._publish_new(bars)
        logger.info("reader_cycle_done", date=str(day), published=published)
        return {"status": "ok", "published": published}

    def _publish_new(self, bars: list[BarEvent]) -> int:
        """Publish bars newer than the last one seen for their (symbol, interval) — no dups."""
        count = 0
        for bar in bars:
            key = (bar.symbol, bar.interval)
            last = self._last_published.get(key)
            if last is not None and bar.ts <= last:
                continue
            self.bus.publish(bar)  # type: ignore[attr-defined]
            self._last_published[key] = bar.ts
            count += 1
        return count

    def start(self) -> None:
        """Start the background hourly job.

        APScheduler is imported lazily so run_cycle stays unit-testable without it installed.
        """
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        sched = BackgroundScheduler(timezone=IST)
        sched.add_job(
            self.run_cycle, CronTrigger.from_crontab(self.cron, timezone=IST),
            id="reader_cycle", max_instances=1, coalesce=True,
        )
        sched.start()
        self._scheduler = sched
        logger.info("reader_scheduler_started", cron=self.cron, intervals=self.intervals)

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)  # type: ignore[attr-defined]
            self._scheduler = None
