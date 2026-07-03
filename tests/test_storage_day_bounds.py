"""_ist_day_bounds: the query-window fix for the "EOD reader publishes 0 bars" bug.

Day-interval bars are stored at IST midnight of the trading session, which is
18:30 UTC of the PREVIOUS calendar date. A boundary built from raw UTC midnight
excludes that row for any single-day window — invisible in wide multi-year backtest
ranges, but silently zeroes a live "give me today's bar" query every single day.
"""
from datetime import date, datetime, timezone

from trading_system.data.storage import _ist_day_bounds

_UTC = timezone.utc


def test_single_day_window_includes_the_actual_stored_ist_midnight_bar():
    # This IS the bug: today's IST-midnight bar is stored at (today-1) 18:30 UTC.
    today = date(2026, 7, 3)
    stored_bar_ts = datetime(2026, 7, 2, 18, 30, 0, tzinfo=_UTC)

    from_dt, to_dt = _ist_day_bounds(today, today)
    assert from_dt <= stored_bar_ts < to_dt


def test_old_utc_midnight_boundary_would_have_missed_it():
    """Documents the exact failure mode that shipped: a naive UTC-midnight boundary
    excludes the row _ist_day_bounds now correctly includes."""
    today = date(2026, 7, 3)
    stored_bar_ts = datetime(2026, 7, 2, 18, 30, 0, tzinfo=_UTC)
    naive_from = datetime(today.year, today.month, today.day, tzinfo=_UTC)
    assert not (naive_from <= stored_bar_ts)  # confirms the old code missed it


def test_intraday_bars_unaffected_by_the_ist_anchored_window():
    # NSE session 09:15-15:30 IST = 03:45-10:00 UTC on the SAME UTC calendar date —
    # must still land inside the window after the fix.
    d = date(2026, 7, 3)
    session_open = datetime(2026, 7, 3, 3, 45, 0, tzinfo=_UTC)
    session_close = datetime(2026, 7, 3, 10, 0, 0, tzinfo=_UTC)
    from_dt, to_dt = _ist_day_bounds(d, d)
    assert from_dt <= session_open < to_dt
    assert from_dt <= session_close < to_dt


def test_multi_day_range_still_covers_every_days_bar():
    from_d, to_d = date(2026, 6, 29), date(2026, 7, 3)
    from_dt, to_dt = _ist_day_bounds(from_d, to_d)
    for d in (from_d, date(2026, 6, 30), date(2026, 7, 1), date(2026, 7, 2), to_d):
        stored_ts = datetime(d.year, d.month, d.day, 18, 30, 0, tzinfo=_UTC) - _one_day()
        assert from_dt <= stored_ts < to_dt


def _one_day():
    from datetime import timedelta
    return timedelta(days=1)
