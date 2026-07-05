"""ReplayerCore: session ordering, pacing hooks, analyst-hook cadence, alerting —
all injected fakes, no Redis/DB/real time."""
from datetime import UTC, date, datetime

from trading_system.agents.replayer.core import ReplayerCore, ReplayPlan
from trading_system.replay.events import BarEvent

_UTC = UTC
D1, D2, D3 = date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3)


def _bar(day: date, hh: int, mm: int, symbol: str, interval: str = "day") -> BarEvent:
    return BarEvent(
        ts=datetime(day.year, day.month, day.day, hh, mm, tzinfo=_UTC), symbol=symbol,
        interval=interval, instrument_token=1, open=100.0, high=101.0, low=99.0,
        close=100.5, volume=1000, features={},
    )


class Harness:
    def __init__(self, bars_by_day: dict):
        self.bars_by_day = bars_by_day
        self.published: list[BarEvent] = []
        self.sleeps: list[float] = []
        self.analyst_days: list[date] = []
        self.alerts: list[str] = []

    def core(self, analyst=False):
        return ReplayerCore(
            publish=self.published.append,
            load_bars=lambda interval, day: iter(self.bars_by_day.get((interval, day), [])),
            trading_days=lambda f, t: [d for d in (D1, D2, D3) if f <= d <= t],
            sleep=self.sleeps.append,
            analyst_hook=self.analyst_days.append if analyst else None,
            alert=lambda kind, detail: self.alerts.append(kind),
        )


def test_day_bars_published_in_order_with_day_delay_between_sessions():
    h = Harness({
        ("day", D1): [_bar(D1, 0, 0, "AAA"), _bar(D1, 0, 0, "BBB")],
        ("day", D2): [_bar(D2, 0, 0, "AAA")],
    })
    summary = h.core().run(ReplayPlan(D1, D2, intervals=("day",), day_delay=4.0))
    assert summary == {"sessions": 2, "bars": 3}
    assert [b.symbol for b in h.published] == ["AAA", "BBB", "AAA"]
    assert h.sleeps == [4.0, 4.0]                     # one day-delay per session


def test_intraday_bars_sleep_between_distinct_timestamps_only():
    bars = [
        _bar(D1, 3, 45, "AAA", "5minute"), _bar(D1, 3, 45, "BBB", "5minute"),  # same ts
        _bar(D1, 3, 50, "AAA", "5minute"),                                      # new ts
    ]
    h = Harness({("5minute", D1): bars})
    h.core().run(ReplayPlan(D1, D1, intervals=("5minute",),
                            day_delay=1.0, intraday_delay=0.05))
    # exactly ONE intraday pause (between the two distinct timestamps) + day delay
    assert h.sleeps == [0.05, 1.0]
    assert len(h.published) == 3


def test_analyst_hook_called_once_per_day_before_bars():
    h = Harness({("day", D1): [_bar(D1, 0, 0, "AAA")]})
    h.core(analyst=True).run(ReplayPlan(D1, D1, with_news=True, day_delay=0.0))
    assert h.analyst_days == [D1]


def test_analyst_hook_failure_does_not_stop_replay():
    h = Harness({("day", D1): [_bar(D1, 0, 0, "AAA")], ("day", D2): [_bar(D2, 0, 0, "AAA")]})

    def boom(day):
        raise RuntimeError("news api down")

    core = ReplayerCore(
        publish=h.published.append,
        load_bars=lambda i, d: iter(h.bars_by_day.get((i, d), [])),
        trading_days=lambda f, t: [D1, D2],
        sleep=h.sleeps.append,
        analyst_hook=boom,
        alert=lambda k, d: h.alerts.append(k),
    )
    summary = core.run(ReplayPlan(D1, D2, with_news=True, day_delay=0.0))
    assert summary["sessions"] == 2 and summary["bars"] == 2   # bars still flowed


def test_hook_skipped_when_with_news_false():
    h = Harness({("day", D1): [_bar(D1, 0, 0, "AAA")]})
    h.core(analyst=True).run(ReplayPlan(D1, D1, with_news=False, day_delay=0.0))
    assert h.analyst_days == []


def test_done_alert_emitted():
    h = Harness({("day", D1): [_bar(D1, 0, 0, "AAA")]})
    h.core().run(ReplayPlan(D1, D1, day_delay=0.0))
    assert h.alerts == ["replay_done"]
