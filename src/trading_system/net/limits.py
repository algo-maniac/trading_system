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


_SPEND_TTL_SECONDS = 40 * 86_400  # a month key outlives its month by ~10 days for ops
_MICRO = 1_000_000                # store integer micro-dollars — no float drift in Redis


class MonthlySpend:
    """Cumulative monthly USD spend tracker for a metered API (the LLM).

    Unlike DailyBudget (call counts, known before the call), real cost is only known
    AFTER a call returns (from response.usage). So the contract is:
      precheck()  — BEFORE the call: raise BudgetExhausted once the month's total has
                    reached the cap. One in-flight call can overshoot by its own cost
                    (~$0.003 for a Haiku review) — accepted, keeps the hot path simple.
      add_usd()   — AFTER the call: record the actual cost; returns the new total.
      crossed_alert_threshold() — True exactly once per month (SETNX flag) when the
                    total passes the alert line, so the caller raises one bus alert,
                    not one per call.
    """

    def __init__(self, api: str, cap_usd: float, alert_usd: float = 0.0,
                 client: "redis.Redis | None" = None) -> None:
        self._api = api
        self._cap_usd = cap_usd
        self._alert_usd = alert_usd
        self._r = client or redis.Redis.from_url(get_settings().redis_url, decode_responses=True)

    def _key(self) -> str:
        return f"spend:{self._api}:{datetime.now(_IST).strftime('%Y-%m')}"

    def total_usd(self) -> float:
        value = self._r.get(self._key())
        return (int(value) if value else 0) / _MICRO

    def precheck(self) -> None:
        if self.total_usd() >= self._cap_usd:
            logger.warning("spend_cap_reached", api=self._api, cap_usd=self._cap_usd)
            raise BudgetExhausted(self._api, int(self._cap_usd))

    def add_usd(self, cost_usd: float) -> float:
        key = self._key()
        new_total = int(self._r.incrby(key, max(0, round(cost_usd * _MICRO))))
        self._r.expire(key, _SPEND_TTL_SECONDS)
        return new_total / _MICRO

    def crossed_alert_threshold(self, total_usd: float) -> bool:
        if self._alert_usd <= 0 or total_usd < self._alert_usd:
            return False
        # SETNX flag: only the call that first crosses the line gets True.
        return bool(self._r.set(f"{self._key()}:alerted", "1", nx=True, ex=_SPEND_TTL_SECONDS))
