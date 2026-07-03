"""NSE trading calendar helpers backed by pandas_market_calendars (NSE).

NSE session: 09:15 – 15:30 IST, Monday–Friday, excluding NSE holidays.
For minute data, each trading day has 375 one-minute bars (09:15 to 15:29 inclusive).
"""
from datetime import date
from functools import lru_cache

import pandas as pd
import pandas_market_calendars as mcal

from trading_system.observability import get_logger

logger = get_logger(__name__)


@lru_cache(maxsize=1)
def _nse() -> mcal.MarketCalendar:
    return mcal.get_calendar("NSE")


def is_trading_day(d: date) -> bool:
    schedule = _nse().schedule(start_date=str(d), end_date=str(d))
    return not schedule.empty


def get_trading_days(from_date: date, to_date: date) -> list[date]:
    schedule = _nse().schedule(start_date=str(from_date), end_date=str(to_date))
    return [ts.date() for ts in schedule.index]


def get_expected_minute_bars(trading_date: date) -> int:
    """NSE: 09:15–15:29 = 375 one-minute bars per session."""
    return 375


# Bars per NSE session (375 one-minute bars, 09:15–15:30) by interval.
# 60-minute is 6 because the final session block (15:00–15:30) is a partial bar.
_BARS_PER_SESSION: dict[str, int] = {
    "day": 1,
    "minute": 375,
    "5minute": 75,
    "15minute": 25,
    "60minute": 6,
}


def bars_per_session(interval: str) -> int:
    """Number of bars in one NSE trading session for the given interval."""
    return _BARS_PER_SESSION.get(interval, 1)


def expected_bar_count(from_date: date, to_date: date, interval: str) -> int:
    days = get_trading_days(from_date, to_date)
    return len(days) * bars_per_session(interval)
