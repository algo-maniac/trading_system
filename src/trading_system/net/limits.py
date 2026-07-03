"""Daily call budgets for external APIs (StockInsights, LLM, ...).

A Redis counter keyed per (api, IST calendar date) — shared across agents and restarts,
which an in-process counter can't be. `try_spend` is the only mutation: atomic INCRBY,
then refund-and-refuse if the increment crossed the cap (two budget users racing can
each see a fleeting overshoot of at most their own `n`; for API-politeness budgets that
slack is fine and keeps the hot path to one round-trip).

Callers treat `BudgetExhausted` as "skip this work + alert", never as a crash — an
exhausted news budget must degrade the day's analysis, not kill the agent.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import redis

from trading_system.config import get_settings
from trading_system.observability import get_logger

logger = get_logger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
_TTL_SECONDS = 3 * 86_400  # keys self-expire; 3 days keeps yesterday visible for ops


class BudgetExhausted(Exception):
    def __init__(self, api: str, cap: int) -> None:
        super().__init__(f"daily budget exhausted for '{api}' (cap {cap})")
        self.api = api
        self.cap = cap


class DailyBudget:
    def __init__(self, api: str, cap: int, client: "redis.Redis | None" = None) -> None:
        self._api = api
        self._cap = cap
        self._r = client or redis.Redis.from_url(get_settings().redis_url, decode_responses=True)

    def _key(self) -> str:
        return f"budget:{self._api}:{datetime.now(_IST).date().isoformat()}"

    def spent(self) -> int:
        value = self._r.get(self._key())
        return int(value) if value else 0

    def try_spend(self, n: int = 1) -> None:
        """Consume `n` calls from today's budget or raise BudgetExhausted."""
        key = self._key()
        new_total = int(self._r.incrby(key, n))
        self._r.expire(key, _TTL_SECONDS)
        if new_total > self._cap:
            self._r.decrby(key, n)  # refund — the work will not happen
            logger.warning("budget_exhausted", api=self._api, cap=self._cap)
            raise BudgetExhausted(self._api, self._cap)
