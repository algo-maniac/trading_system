"""_tick_timestamp: normalizes Kite's naive-IST exchange_timestamp to tz-aware,
without shifting the wall-clock hour/minute the aggregator's session bucketing
depends on (09:15 open etc.)."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from trading_system.agents.reader.live_app import _tick_timestamp

IST = ZoneInfo("Asia/Kolkata")


def test_naive_exchange_timestamp_gets_ist_tzinfo_not_shifted():
    naive = datetime(2026, 7, 3, 9, 15, 0)  # Kite's convention: naive, IST wall-clock
    ts = _tick_timestamp({"exchange_timestamp": naive})
    assert ts.tzinfo is not None
    assert (ts.hour, ts.minute) == (9, 15)  # wall-clock preserved, NOT UTC-shifted


def test_already_aware_exchange_timestamp_passed_through():
    aware = datetime(2026, 7, 3, 9, 15, 0, tzinfo=timezone.utc)
    assert _tick_timestamp({"exchange_timestamp": aware}) is aware


def test_missing_exchange_timestamp_falls_back_to_aware_now():
    ts = _tick_timestamp({})
    assert ts.tzinfo is not None
