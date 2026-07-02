"""Unit tests for NSE trading calendar helpers."""
from datetime import date

import pytest

from trading_system.data.calendar import (
    expected_bar_count,
    get_expected_minute_bars,
    get_trading_days,
    is_trading_day,
)


def test_weekday_is_trading_day():
    # 2024-01-02 was a Tuesday — a normal NSE trading day
    assert is_trading_day(date(2024, 1, 2))


def test_saturday_is_not_trading_day():
    assert not is_trading_day(date(2024, 1, 6))


def test_sunday_is_not_trading_day():
    assert not is_trading_day(date(2024, 1, 7))


def test_republic_day_is_nse_holiday():
    # 26 January 2024 (Friday) — Republic Day, NSE closed
    assert not is_trading_day(date(2024, 1, 26))


def test_trading_days_no_weekends():
    days = get_trading_days(date(2024, 1, 1), date(2024, 1, 31))
    assert all(d.weekday() < 5 for d in days), "Weekends slipped into trading days"


def test_trading_days_excludes_republic_day():
    days = get_trading_days(date(2024, 1, 26), date(2024, 1, 26))
    assert len(days) == 0


def test_trading_days_january_2024_reasonable_count():
    days = get_trading_days(date(2024, 1, 1), date(2024, 1, 31))
    # Jan 2024: ~23 weekdays, minus Republic Day (Jan 26) = 22 trading days
    assert 20 <= len(days) <= 23, f"Unexpected trading day count: {len(days)}"


def test_trading_days_single_valid_day():
    days = get_trading_days(date(2024, 1, 2), date(2024, 1, 2))
    assert len(days) == 1
    assert days[0] == date(2024, 1, 2)


def test_minute_bars_per_day():
    assert get_expected_minute_bars(date(2024, 1, 2)) == 375


def test_expected_bar_count_day():
    count = expected_bar_count(date(2024, 1, 2), date(2024, 1, 2), "day")
    assert count == 1


def test_expected_bar_count_minute():
    # One trading day × 375 minute bars
    count = expected_bar_count(date(2024, 1, 2), date(2024, 1, 2), "minute")
    assert count == 375


@pytest.mark.parametrize(
    "interval,per_session",
    [("5minute", 75), ("15minute", 25), ("60minute", 6), ("day", 1), ("minute", 375)],
)
def test_bars_per_session(interval, per_session):
    from trading_system.data.calendar import bars_per_session
    assert bars_per_session(interval) == per_session


@pytest.mark.parametrize(
    "interval,per_session",
    [("5minute", 75), ("15minute", 25), ("60minute", 6)],
)
def test_expected_bar_count_intraday(interval, per_session):
    # One trading day × bars-per-session
    count = expected_bar_count(date(2024, 1, 2), date(2024, 1, 2), interval)
    assert count == per_session
