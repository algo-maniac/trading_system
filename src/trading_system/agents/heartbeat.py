"""Periodic liveness heartbeat for long-running agents.

A sleeve/strategy that hasn't fired all day is indistinguishable in the logs from a
consumer that's silently stuck — both produce zero output. `Heartbeat.tick()` closes
that gap: called on every loop iteration, it logs a summary at most once per
`interval_seconds`, cheap enough to call unconditionally.
"""
import time
from collections.abc import Callable

from trading_system.observability import get_logger

logger = get_logger(__name__)


class Heartbeat:
    def __init__(self, interval_seconds: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._interval = interval_seconds
        self._clock = clock
        self._last = clock()

    def tick(self, event: str, **fields: object) -> bool:
        """Log `event` with `fields` if the interval has elapsed. Returns whether it fired."""
        now = self._clock()
        if now - self._last < self._interval:
            return False
        self._last = now
        logger.info(event, **fields)
        return True
