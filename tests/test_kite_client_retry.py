"""Kite historical fetch: jittered backoff bounds, retry-then-succeed, exhausted retries."""
import random
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from kiteconnect.exceptions import NetworkException

from trading_system.data.kite_client import _jittered_delay, fetch_historical


def test_jittered_delay_stays_within_equal_jitter_bounds():
    rng = random.Random(42)
    for attempt in range(5):
        exp = 1.0 * (2 ** attempt)
        d = _jittered_delay(1.0, attempt, rand=rng)
        assert exp / 2.0 <= d <= exp


def test_jittered_delay_varies_across_calls():
    rng = random.Random(7)
    delays = {_jittered_delay(1.0, 2, rand=rng) for _ in range(20)}
    assert len(delays) > 1  # not constant — jitter is actually randomizing


@patch("trading_system.data.kite_client.time.sleep")
def test_fetch_retries_then_succeeds(mock_sleep):
    kite = MagicMock()
    kite.historical_data.side_effect = [
        NetworkException("timeout"),
        NetworkException("timeout"),
        [{"date": "2024-01-01", "close": 100}],
    ]
    result = fetch_historical(kite, 123, date(2024, 1, 1), date(2024, 1, 1), "day")
    assert result == [{"date": "2024-01-01", "close": 100}]
    assert kite.historical_data.call_count == 3
    assert mock_sleep.call_count == 2  # slept before retry 2 and 3, not after success


@patch("trading_system.data.kite_client.time.sleep")
def test_fetch_raises_after_max_attempts(mock_sleep):
    kite = MagicMock()
    kite.historical_data.side_effect = NetworkException("down")
    with pytest.raises(NetworkException):
        fetch_historical(kite, 123, date(2024, 1, 1), date(2024, 1, 1), "day")
    from trading_system.config import get_settings
    assert kite.historical_data.call_count == get_settings().kite_retry_max
