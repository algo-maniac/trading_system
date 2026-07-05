"""ReporterCore: data-driven period detection (daily/ISO-week/month rollovers),
report assembly + markdown, baseline selection — all DB-free via injected fakes."""
from datetime import date

from trading_system.agents.reporter.core import (
    Period,
    ReporterCore,
    ReporterDeps,
    pending_periods,
    period_for,
    render_markdown,
)

# July 2026: Wed 1, Thu 2, Fri 3 | Mon 6 ... | week of Mon Jun 29 spans the boundary


def test_pending_daily_needs_a_later_date():
    dates = [date(2026, 7, 1), date(2026, 7, 2)]
    due = pending_periods(dates, reported=set())
    daily = [p for p in due if p.period_type == "daily"]
    assert [p.start for p in daily] == [date(2026, 7, 1)]   # day 2 still open


def test_pending_daily_skips_already_reported():
    dates = [date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3)]
    due = pending_periods(dates, reported={("daily", date(2026, 7, 1))})
    daily = [p.start for p in due if p.period_type == "daily"]
    assert daily == [date(2026, 7, 2)]


def test_pending_weekly_closes_on_new_iso_week():
    # Fri Jul 3 (week of Mon Jun 29) then Mon Jul 6 → the June-29 week is complete
    dates = [date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3), date(2026, 7, 6)]
    due = pending_periods(dates, reported=set())
    weekly = [p for p in due if p.period_type == "weekly"]
    assert len(weekly) == 1
    assert weekly[0].start == date(2026, 6, 29) and weekly[0].end == date(2026, 7, 5)


def test_pending_monthly_closes_on_new_month():
    dates = [date(2026, 6, 29), date(2026, 6, 30), date(2026, 7, 1)]
    due = pending_periods(dates, reported=set())
    monthly = [p for p in due if p.period_type == "monthly"]
    assert len(monthly) == 1
    assert monthly[0].start == date(2026, 6, 1) and monthly[0].end == date(2026, 6, 30)


def test_pending_iso_week_year_boundary():
    # Wed Dec 30 2026 and Mon Jan 4 2027: week of Mon Dec 28 completes at year turn
    dates = [date(2026, 12, 30), date(2027, 1, 4)]
    due = pending_periods(dates, reported=set())
    weekly = [p for p in due if p.period_type == "weekly"]
    assert weekly[0].start == date(2026, 12, 28) and weekly[0].end == date(2027, 1, 3)


def test_pending_empty_and_single_date():
    assert pending_periods([], set()) == []
    assert pending_periods([date(2026, 7, 1)], set()) == []   # nothing complete yet


def test_period_for_resolves_containing_period():
    assert period_for("daily", date(2026, 7, 2)) == Period("daily", date(2026, 7, 2),
                                                           date(2026, 7, 2))
    w = period_for("weekly", date(2026, 7, 2))
    assert w.start == date(2026, 6, 29) and w.end == date(2026, 7, 5)
    m = period_for("monthly", date(2026, 7, 15))
    assert m.start == date(2026, 7, 1) and m.end == date(2026, 7, 31)


def _deps(equity, closed=None, opens=None):
    return ReporterDeps(
        equity_rows=lambda f, t: [r for r in equity if f <= r[0] <= t],
        closed_positions=lambda f, t: closed or [],
        total_fees=lambda f, t: 123.0,
        open_positions=lambda: opens or [],
        signal_count=lambda f, t: 10,
        order_count=lambda f, t: 7,
        fill_count=lambda f, t: 6,
        admissions=lambda f, t: ["AWL"],
        news_count=lambda f, t: 42,
        budget_snapshot=lambda: {"llm_month_usd": 0.05, "stockinsights_today": 12},
    )


def test_build_report_uses_prior_day_as_baseline():
    equity = [
        (date(2026, 7, 1), "momentum", 300_000.0),
        (date(2026, 7, 1), "stabilizer", 500_000.0),
        (date(2026, 7, 2), "momentum", 310_000.0),
        (date(2026, 7, 2), "stabilizer", 505_000.0),
    ]
    core = ReporterCore(_deps(equity), namespace="live")
    payload, md = core.build_report(Period("daily", date(2026, 7, 2), date(2026, 7, 2)))
    assert payload["equity_start"] == 800_000.0          # prior day's book, not day-2's
    assert payload["equity_end"] == 815_000.0
    assert abs(payload["period_return"] - 815_000.0 / 800_000.0 + 1.0) < 1e-9
    assert payload["sleeves"]["momentum"]["return"] == (310_000.0 / 300_000.0 - 1.0)
    assert "Daily report — 2026-07-02" in md


def test_build_report_carries_missing_sleeve_forward():
    """A sleeve with no snapshot on a date keeps its last value in the book sum."""
    equity = [
        (date(2026, 7, 1), "momentum", 300_000.0),
        (date(2026, 7, 1), "stabilizer", 500_000.0),
        (date(2026, 7, 2), "momentum", 310_000.0),   # stabilizer missing on day 2
    ]
    core = ReporterCore(_deps(equity))
    payload, _ = core.build_report(Period("daily", date(2026, 7, 2), date(2026, 7, 2)))
    assert payload["equity_end"] == 810_000.0            # 310k + carried 500k


def test_build_report_trade_stats_and_markdown_sections():
    closed = [
        {"sleeve": "stabilizer", "symbol": "INFY", "avg_price": 1500.0,
         "realized_pnl": 2_000.0, "opened_ts": "2026-06-25", "closed_ts": "2026-07-01",
         "exit_reason": "trend_break"},
        {"sleeve": "stabilizer", "symbol": "TCS", "avg_price": 3500.0,
         "realized_pnl": -1_000.0, "opened_ts": "2026-06-26", "closed_ts": "2026-07-02",
         "exit_reason": "max_hold"},
    ]
    opens = [{"sleeve": "momentum", "symbol": "RELIANCE", "qty": 10,
              "avg_price": 2900.0, "opened_ts": "2026-06-20"}]
    equity = [(date(2026, 7, d), "momentum", 300_000.0 + d * 1000) for d in (1, 2, 3)]
    core = ReporterCore(_deps(equity, closed=closed, opens=opens))
    payload, md = core.build_report(Period("weekly", date(2026, 6, 29), date(2026, 7, 5)))
    t = payload["trades"]
    assert t["count"] == 2 and t["win_rate"] == 0.5 and t["profit_factor"] == 2.0
    assert t["exit_reasons"] == {"max_hold": 1, "trend_break": 1}
    for section in ("## Book", "## Sleeves", "## Trades", "## Open positions",
                    "## Activity"):
        assert section in md
    assert "RELIANCE" in md and "AWL" in md


def test_build_report_empty_period_degrades():
    core = ReporterCore(_deps([]))
    payload, md = core.build_report(Period("daily", date(2026, 7, 2), date(2026, 7, 2)))
    assert payload["equity_end"] is None and payload["period_return"] is None
    assert "n/a" in md                                   # renders, doesn't crash


def test_markdown_deterministic():
    equity = [(date(2026, 7, 1), "m", 100.0), (date(2026, 7, 2), "m", 110.0)]
    core = ReporterCore(_deps(equity))
    p1, md1 = core.build_report(Period("daily", date(2026, 7, 2), date(2026, 7, 2)))
    p2, md2 = core.build_report(Period("daily", date(2026, 7, 2), date(2026, 7, 2)))
    assert md1 == md2 and p1 == p2
    assert md1 == render_markdown(p1)
