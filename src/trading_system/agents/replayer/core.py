"""ReplayerCore: conduct an accelerated historic replay session by session.

The replayer is the ONLY replay-specific process — it stands in for both readers
(publishing stored bars onto the same bus stream) and the analyst's cron (driving
the sweep inline with a virtual clock). Strategist, Trader and Reporter run their
normal binaries against the replay namespace and cannot tell the difference.

Pacing contract per simulated trading day D:
  1. analyst_hook(D)                — optional news sweep/admissions/focus (virtual now)
  2. publish day-interval bars      — one burst, exactly like the EOD reader
  3. publish 5-min bars (optional)  — sleeping intraday_delay between timestamp groups
  4. sleep day_delay                — MUST exceed the strategist's idle-flush so the
                                      cross-sectional batch fires and its orders are
                                      journaled before tomorrow's bars deliver fills

Everything is injected (publish, bar loading, calendar, sleep, hooks) so the pacing
and ordering logic is unit-testable without Redis, Postgres or real time.
"""
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date

from trading_system.observability import get_logger
from trading_system.replay.events import BarEvent

logger = get_logger(__name__)


@dataclass(frozen=True)
class ReplayPlan:
    from_date: date
    to_date: date
    intervals: tuple = ("day",)          # ("day",) or ("day", "5minute")
    day_delay: float = 4.0
    intraday_delay: float = 0.05
    with_news: bool = False


class ReplayerCore:
    def __init__(
        self,
        publish: Callable[[BarEvent], object],
        load_bars: Callable[[str, date], Iterable[BarEvent]],   # (interval, day) -> bars
        trading_days: Callable[[date, date], list],             # calendar
        sleep: Callable[[float], None],
        analyst_hook: "Callable[[date], object] | None" = None,
        alert: "Callable[[str, str], None] | None" = None,
        heartbeat: "object | None" = None,
    ) -> None:
        self._publish = publish
        self._load_bars = load_bars
        self._trading_days = trading_days
        self._sleep = sleep
        self._analyst_hook = analyst_hook
        self._alert = alert
        self._heartbeat = heartbeat
        self.bars_published = 0
        self.days_replayed = 0

    def run(self, plan: ReplayPlan) -> dict:  # type: ignore[type-arg]
        days = self._trading_days(plan.from_date, plan.to_date)
        logger.info("replay_start", from_=str(plan.from_date), to=str(plan.to_date),
                    sessions=len(days), intervals=list(plan.intervals),
                    with_news=plan.with_news)
        for day in days:
            if plan.with_news and self._analyst_hook is not None:
                try:
                    self._analyst_hook(day)
                except Exception as exc:
                    # News is enrichment; a bad sweep must not kill the replay.
                    logger.error("replay_analyst_failed", day=str(day), error=str(exc))
            if "day" in plan.intervals:
                self.bars_published += self._publish_burst(self._load_bars("day", day))
            if "5minute" in plan.intervals:
                self.bars_published += self._publish_paced(
                    self._load_bars("5minute", day), plan.intraday_delay)
            self.days_replayed += 1
            if self._heartbeat is not None:
                self._heartbeat.tick("replay_progress", day=str(day),  # type: ignore[attr-defined]
                                     days=self.days_replayed, bars=self.bars_published)
            self._sleep(plan.day_delay)
        summary = {"sessions": self.days_replayed, "bars": self.bars_published}
        logger.info("replay_done", **summary)
        if self._alert is not None:
            self._alert("replay_done", f"{summary['sessions']} sessions, "
                                       f"{summary['bars']} bars")
        return summary

    # --- internals -------------------------------------------------------------

    def _publish_burst(self, bars: Iterable[BarEvent]) -> int:
        n = 0
        for bar in bars:
            self._publish(bar)
            n += 1
        return n

    def _publish_paced(self, bars: Iterable[BarEvent], delay: float) -> int:
        """Publish bars sleeping `delay` between DISTINCT timestamps (bars within one
        timestamp group — the cross-section — go out back-to-back)."""
        n = 0
        current_ts = None
        for bar in bars:
            if current_ts is not None and bar.ts != current_ts:
                self._sleep(delay)
            current_ts = bar.ts
            self._publish(bar)
            n += 1
        return n
