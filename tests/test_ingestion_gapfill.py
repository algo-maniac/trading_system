"""Gap-aware ingestion: plan_fetch_ranges only requests trading days not already covered.

Uses the real NSE calendar (deterministic) but monkeypatches the DB lookups so no
database is needed. `today` is injected to control the always-refetch-current-day rule.
"""
from datetime import date

import trading_system.data.ingestion as ing
from trading_system.data.calendar import bars_per_session, get_trading_days
from trading_system.data.ingestion import plan_fetch_ranges

# A historical window well in the past so the default `today` rule never interferes.
FROM = date(2024, 1, 1)
TO = date(2024, 1, 31)
FAR_FUTURE = date(2030, 1, 1)
TRADING_DAYS = get_trading_days(FROM, TO)
FULL_5MIN = bars_per_session("5minute")  # 75

# Shorthand: a prior run that covered the whole window.
_PRIOR_RUN = [(FROM, TO)]


def _patch(monkeypatch, counts: dict[date, int], prior_runs=None) -> None:
    monkeypatch.setattr(ing, "get_ohlcv_bar_counts", lambda *a, **k: counts)
    monkeypatch.setattr(ing, "get_ingested_ranges", lambda *a, **k: prior_runs or [])


def test_fully_covered_returns_no_ranges(monkeypatch):
    full = {d: FULL_5MIN for d in TRADING_DAYS}
    _patch(monkeypatch, full)
    assert plan_fetch_ranges(None, 1, "5minute", FROM, TO, today=FAR_FUTURE) == []


def test_nothing_stored_returns_one_spanning_range(monkeypatch):
    _patch(monkeypatch, {})
    ranges = plan_fetch_ranges(None, 1, "5minute", FROM, TO, today=FAR_FUTURE)
    assert ranges == [(TRADING_DAYS[0], TRADING_DAYS[-1])]


def test_middle_gap_splits_into_two_ranges(monkeypatch):
    # Cover everything except a contiguous block in the middle → that block is one range.
    gap = TRADING_DAYS[5:8]
    counts = {d: FULL_5MIN for d in TRADING_DAYS if d not in gap}
    _patch(monkeypatch, counts)
    ranges = plan_fetch_ranges(None, 1, "5minute", FROM, TO, today=FAR_FUTURE)
    assert ranges == [(gap[0], gap[-1])]


def test_two_separate_gaps_yield_two_ranges(monkeypatch):
    # Miss the first two and last two days; cover the middle → two distinct ranges.
    covered = set(TRADING_DAYS[2:-2])
    counts = {d: FULL_5MIN for d in covered}
    _patch(monkeypatch, counts)
    ranges = plan_fetch_ranges(None, 1, "5minute", FROM, TO, today=FAR_FUTURE)
    assert ranges == [
        (TRADING_DAYS[0], TRADING_DAYS[1]),
        (TRADING_DAYS[-2], TRADING_DAYS[-1]),
    ]


def test_incomplete_day_is_refetched(monkeypatch):
    # One day has only a handful of bars (< 90% of a session) → must be re-fetched.
    counts = {d: FULL_5MIN for d in TRADING_DAYS}
    counts[TRADING_DAYS[4]] = 10
    _patch(monkeypatch, counts)
    ranges = plan_fetch_ranges(None, 1, "5minute", FROM, TO, today=FAR_FUTURE)
    assert ranges == [(TRADING_DAYS[4], TRADING_DAYS[4])]


def test_current_day_always_refetched_even_if_full(monkeypatch):
    # today is the last (covered) trading day → still planned (session may be live).
    today = TRADING_DAYS[-1]
    counts = {d: FULL_5MIN for d in TRADING_DAYS}
    _patch(monkeypatch, counts)
    ranges = plan_fetch_ranges(None, 1, "5minute", FROM, TO, today=today)
    assert ranges == [(today, today)]


def test_daily_interval_threshold_is_one_bar(monkeypatch):
    # For 'day', one stored bar = covered; a missing day (count 0) is fetched.
    counts = {d: 1 for d in TRADING_DAYS}
    missing = TRADING_DAYS[3]
    del counts[missing]
    _patch(monkeypatch, counts)
    ranges = plan_fetch_ranges(None, 1, "day", FROM, TO, today=FAR_FUTURE)
    assert ranges == [(missing, missing)]


def test_confirmed_holiday_not_refetched_on_second_run(monkeypatch):
    # Simulates a re-run: all real trading days have bars; one calendar day has 0 bars
    # (NSE holiday Kite never returns data for) but a prior successful run covered the range.
    # → gap-fill must NOT create a fetch range for that holiday date.
    holiday = TRADING_DAYS[5]
    counts = {d: 1 for d in TRADING_DAYS if d != holiday}   # holiday has 0 bars
    _patch(monkeypatch, counts, prior_runs=_PRIOR_RUN)       # prior run covered the window
    ranges = plan_fetch_ranges(None, 1, "day", FROM, TO, today=FAR_FUTURE)
    assert ranges == []   # holiday treated as confirmed closure, not a gap


def test_unfetched_zero_bar_day_is_still_planned(monkeypatch):
    # Same situation but NO prior run → the 0-bar day is genuinely never fetched.
    holiday = TRADING_DAYS[5]
    counts = {d: 1 for d in TRADING_DAYS if d != holiday}
    _patch(monkeypatch, counts, prior_runs=[])   # no prior runs
    ranges = plan_fetch_ranges(None, 1, "day", FROM, TO, today=FAR_FUTURE)
    assert ranges == [(holiday, holiday)]
