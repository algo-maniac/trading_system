"""Rate-limited, chunked, retrying wrapper around KiteConnect.historical_data().

Kite historical API constraints:
- ~3 requests/second (enforced by token-bucket rate limiter below)
- Minute data: max ~60 calendar days per request
- Day/week/month: much larger windows allowed

All raw API responses are archived as Parquet before any transformation
so re-ingestion is possible without hitting Kite again.
"""
import random
import threading
import time
from collections.abc import Iterator
from datetime import date, timedelta

from kiteconnect import KiteConnect
from kiteconnect.exceptions import DataException, NetworkException

from trading_system.config import get_settings
from trading_system.observability import get_logger

logger = get_logger(__name__)


def _jittered_delay(base_delay: float, attempt: int, rand: random.Random | None = None) -> float:
    """Equal-jitter exponential backoff: half the exponential delay is guaranteed (so we
    never retry near-instantly and hammer a struggling API), the other half is randomized
    (so N parallel ingest_workers retrying the same failure don't all wake up on the exact
    same tick and re-collide — a real risk here since ingest_workers share one rate limiter).
    """
    r = rand or random
    exp_delay = base_delay * (2 ** attempt)
    half = exp_delay / 2.0
    return half + r.uniform(0, half)


# Conservative max calendar-day windows per interval
_INTERVAL_MAX_DAYS: dict[str, int] = {
    "minute":    60,
    "3minute":   60,
    "5minute":   60,
    "10minute":  60,
    "15minute":  60,
    "30minute":  60,
    "60minute":  200,
    "day":       2000,
    "week":      2000,
    "month":     2000,
}


class _TokenBucket:
    """Thread-safe token-bucket rate limiter."""

    def __init__(self, rate: float) -> None:
        self._rate = rate
        self._tokens = rate
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self._rate, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                time.sleep(wait)
                self._tokens = 0.0
                self._last = time.monotonic()
            else:
                self._tokens -= 1.0


_limiter: _TokenBucket | None = None
_limiter_lock = threading.Lock()


def _get_limiter() -> _TokenBucket:
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                _limiter = _TokenBucket(get_settings().kite_historical_rps)
    return _limiter


def _date_chunks(from_date: date, to_date: date, interval: str) -> Iterator[tuple[date, date]]:
    max_days = _INTERVAL_MAX_DAYS.get(interval, 60)
    current = from_date
    while current <= to_date:
        yield current, min(current + timedelta(days=max_days - 1), to_date)
        current += timedelta(days=max_days)


def fetch_historical(
    kite: KiteConnect,
    instrument_token: int,
    from_date: date,
    to_date: date,
    interval: str,
) -> list[dict]:  # type: ignore[type-arg]
    """Fetch all candles for a symbol/interval/date-range, chunked and throttled."""
    s = get_settings()
    limiter = _get_limiter()
    results: list[dict] = []  # type: ignore[type-arg]

    for chunk_from, chunk_to in _date_chunks(from_date, to_date, interval):
        for attempt in range(s.kite_retry_max):
            try:
                limiter.acquire()
                records = kite.historical_data(
                    instrument_token,
                    from_date=chunk_from,
                    to_date=chunk_to,
                    interval=interval,
                    continuous=False,
                    oi=False,
                )
                results.extend(records)
                logger.debug(
                    "chunk_fetched",
                    token=instrument_token,
                    interval=interval,
                    from_=chunk_from.isoformat(),
                    to=chunk_to.isoformat(),
                    n=len(records),
                )
                break  # success
            except (NetworkException, DataException) as exc:
                delay = _jittered_delay(s.kite_retry_base_delay, attempt)
                logger.warning(
                    "kite_fetch_retry",
                    token=instrument_token,
                    interval=interval,
                    attempt=attempt + 1,
                    error=str(exc),
                    retry_in_s=delay,
                )
                if attempt == s.kite_retry_max - 1:
                    raise
                time.sleep(delay)

    return results
