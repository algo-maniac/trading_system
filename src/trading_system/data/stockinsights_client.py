"""StockInsights.ai announcements client (Phase 5c news source).

GET {base}/api/in/v0/documents/announcement — Bearer auth, paginated (limit ≤ 50).
Response rows come pre-tagged by their AI (announcement type, sentiment, summaries),
which is exactly the context the admission reviewer needs.

Discipline (this is a metered trial API):
  - every request passes a DailyBudget (Redis-backed, shared across restarts) — an
    exhausted budget raises BudgetExhausted for the CALLER to skip+alert on;
  - ≤ `stockinsights_rps` politeness spacing between requests;
  - equal-jitter exponential retry on 5xx/network (same rationale as kite_client);
  - 4xx raises immediately (bad key/params — retrying can't help);
  - pagination hard-capped so a runaway `total_count` can't drain the budget.

Symbol mapping: each row's `exchange_tickers` may list BSE and NSE entries; we keep the
NSE tradingsymbol (our whole system keys on NSE). Rows with no NSE listing keep
nse_symbol=None — stored for context, never tradable.
"""
import hashlib
import random
import time
from dataclasses import dataclass, field
from datetime import date, datetime

import requests

from trading_system.config import get_settings
from trading_system.net.limits import DailyBudget
from trading_system.observability import get_logger

logger = get_logger(__name__)

_MAX_PAGES_PER_SWEEP = 6      # 6 × 50 = 300 announcements/sweep is plenty
_RETRY_MAX = 4
_RETRY_BASE_DELAY = 1.0


@dataclass(frozen=True)
class NewsItem:
    source_link: str
    source_link_hash: str
    nse_symbol: str | None
    company_name: str
    announcement_type_id: int | None
    announcement_type: str
    sentiment: str
    significant: bool              # StockInsights' own materiality judgment
    summary_header: str
    summary_text: str
    published_ts: datetime | None
    raw: dict = field(default_factory=dict)  # type: ignore[type-arg]


def _hash_link(link: str) -> str:
    return hashlib.sha256(link.encode()).hexdigest()


def parse_item(row: dict) -> NewsItem:  # type: ignore[type-arg]
    """One API row → NewsItem. Tolerant of missing fields — a partial row is still
    worth storing; only source_link is mandatory (it is the dedupe identity).

    Field-location note (learned from LIVE responses, not the docs): the AI tags live
    NESTED under `ai_insights` (sentiment, significance, announcement_type[_id],
    summaries); type_id arrives as a string. We read ai_insights first and fall back
    to top-level names (the shape the docs summary implied) for forward-compat.
    """
    insights = row.get("ai_insights") or {}

    def tag(name: str):  # ai_insights first, top-level fallback
        value = insights.get(name)
        return value if value is not None else row.get(name)

    nse_symbol = None
    for et in row.get("exchange_tickers") or []:
        if (et.get("exchange") or "").upper() == "NSE" and et.get("ticker"):
            nse_symbol = str(et["ticker"]).upper()
            break
    published = None
    raw_ts = row.get("published_date")
    if raw_ts:
        try:
            published = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        except ValueError:
            logger.warning("news_bad_published_date", value=raw_ts)
    type_id = tag("announcement_type_id")
    try:
        type_id = int(type_id) if type_id is not None else None
    except (TypeError, ValueError):
        type_id = None
    link = str(row.get("source_link") or "")
    return NewsItem(
        source_link=link,
        source_link_hash=_hash_link(link),
        nse_symbol=nse_symbol,
        company_name=str(row.get("company_name") or ""),
        announcement_type_id=type_id,
        announcement_type=str(tag("announcement_type") or ""),
        sentiment=str(tag("sentiment") or "neutral").lower(),
        significant=bool(tag("significance")),
        summary_header=str(tag("summary_header") or ""),
        summary_text=str(tag("summary_text") or ""),
        published_ts=published,
        raw=row,
    )


class StockInsightsClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        budget: DailyBudget | None = None,
        session: "requests.Session | None" = None,
    ) -> None:
        s = get_settings()
        self._key = api_key if api_key is not None else s.stockinsights_api_key
        self._base = (base_url or s.stockinsights_base_url).rstrip("/")
        self._budget = budget or DailyBudget("stockinsights", s.stockinsights_daily_budget)
        self._session = session or requests.Session()
        self._min_interval = 1.0 / max(s.stockinsights_rps, 0.1)
        self._last_request = 0.0

    def fetch_announcements(self, from_date: date, to_date: date) -> list[NewsItem]:
        """All announcements in [from_date, to_date], paginated, budget-guarded.

        Raises BudgetExhausted (caller skips + alerts) or requests.HTTPError on 4xx.
        """
        items: list[NewsItem] = []
        for page in range(1, _MAX_PAGES_PER_SWEEP + 1):
            payload = self._get_page(from_date, to_date, page)
            rows = payload.get("data") or payload.get("results") or []
            items.extend(parse_item(r) for r in rows)
            total = int(payload.get("total_count") or 0)
            if len(rows) < 50 or (total and page * 50 >= total):
                break
        logger.info("stockinsights_sweep_fetched", n=len(items),
                    from_=str(from_date), to=str(to_date))
        return items

    # --- internals ---------------------------------------------------------------

    def _get_page(self, from_date: date, to_date: date, page: int) -> dict:  # type: ignore[type-arg]
        self._budget.try_spend(1)
        for attempt in range(_RETRY_MAX):
            self._pace()
            try:
                resp = self._session.get(
                    f"{self._base}/api/in/v0/documents/announcement",
                    headers={"Authorization": f"Bearer {self._key}"},
                    params={"from_date": from_date.isoformat(),
                            "to_date": to_date.isoformat(),
                            "limit": 50, "page": page},
                    timeout=30,
                )
                if resp.status_code >= 500:
                    raise requests.exceptions.RetryError(f"server error {resp.status_code}")
                resp.raise_for_status()   # 4xx → immediate raise, no retry
                return resp.json()  # type: ignore[no-any-return]
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                    requests.exceptions.RetryError) as exc:
                if attempt == _RETRY_MAX - 1:
                    raise
                exp = _RETRY_BASE_DELAY * (2 ** attempt)
                delay = exp / 2 + random.uniform(0, exp / 2)   # equal-jitter, as kite_client
                logger.warning("stockinsights_retry", attempt=attempt + 1,
                               error=str(exc), retry_in_s=round(delay, 1))
                time.sleep(delay)
        raise RuntimeError("unreachable")  # pragma: no cover

    def _pace(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.monotonic()
