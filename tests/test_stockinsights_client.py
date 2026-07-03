"""StockInsights client: parsing, NSE symbol extraction, pagination, budget, retries —
mocked HTTP session, fake in-memory budget (no Redis)."""
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import requests

from trading_system.data.stockinsights_client import (
    NewsItem, StockInsightsClient, parse_item,
)
from trading_system.net.limits import BudgetExhausted


class FakeBudget:
    def __init__(self, cap=100):
        self.cap = cap
        self.spent_n = 0

    def try_spend(self, n=1):
        if self.spent_n + n > self.cap:
            raise BudgetExhausted("stockinsights", self.cap)
        self.spent_n += n


def _row(sym="ZOMATO", link="https://nse.example/1"):
    # Mirrors the REAL response shape (AI tags nested under ai_insights, type_id str)
    return {
        "source_link": link,
        "company_name": f"{sym} Ltd",
        "exchange_tickers": [{"exchange": "BSE", "ticker": "543320"},
                             {"exchange": "NSE", "ticker": sym}],
        "ai_insights": {"announcement_type_id": "4", "announcement_type": "Order Win",
                        "sentiment": "Positive", "significance": True,
                        "summary_header": "h", "summary_text": "t"},
        "published_date": "2026-07-03T10:00:00Z",
    }


def _resp(rows, total):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"data": rows, "total_count": total, "page": 1, "limit": 50}
    r.raise_for_status.return_value = None
    return r


def _client(session, budget=None):
    return StockInsightsClient(api_key="k", base_url="https://api.test",
                               budget=budget or FakeBudget(), session=session)


def test_parse_extracts_nse_symbol_and_normalizes():
    item = parse_item(_row())
    assert item.nse_symbol == "ZOMATO"
    assert item.sentiment == "positive"                    # lowercased
    assert item.published_ts is not None
    assert len(item.source_link_hash) == 64


def test_parse_without_nse_listing_keeps_none():
    row = _row()
    row["exchange_tickers"] = [{"exchange": "BSE", "ticker": "500325"}]
    assert parse_item(row).nse_symbol is None


@patch("trading_system.data.stockinsights_client.time.sleep")
def test_single_page_fetch(mock_sleep):
    session = MagicMock()
    session.get.return_value = _resp([_row()], total=1)
    items = _client(session).fetch_announcements(date(2026, 7, 2), date(2026, 7, 3))
    assert len(items) == 1 and isinstance(items[0], NewsItem)
    assert session.get.call_count == 1
    auth = session.get.call_args.kwargs["headers"]["Authorization"]
    assert auth == "Bearer k"


@patch("trading_system.data.stockinsights_client.time.sleep")
def test_paginates_until_total_reached(mock_sleep):
    session = MagicMock()
    page1 = _resp([_row(link=f"https://x/{i}") for i in range(50)], total=70)
    page2 = _resp([_row(link=f"https://x/{50+i}") for i in range(20)], total=70)
    session.get.side_effect = [page1, page2]
    items = _client(session).fetch_announcements(date(2026, 7, 2), date(2026, 7, 3))
    assert len(items) == 70 and session.get.call_count == 2


@patch("trading_system.data.stockinsights_client.time.sleep")
def test_budget_exhausted_propagates_before_any_http(mock_sleep):
    session = MagicMock()
    with pytest.raises(BudgetExhausted):
        _client(session, budget=FakeBudget(cap=0)).fetch_announcements(
            date(2026, 7, 2), date(2026, 7, 3))
    assert session.get.call_count == 0     # budget guards BEFORE spending a request


@patch("trading_system.data.stockinsights_client.time.sleep")
def test_retries_on_5xx_then_succeeds(mock_sleep):
    session = MagicMock()
    bad = MagicMock()
    bad.status_code = 503
    session.get.side_effect = [bad, _resp([_row()], total=1)]
    items = _client(session).fetch_announcements(date(2026, 7, 2), date(2026, 7, 3))
    assert len(items) == 1 and session.get.call_count == 2


@patch("trading_system.data.stockinsights_client.time.sleep")
def test_4xx_raises_immediately_no_retry(mock_sleep):
    session = MagicMock()
    bad = MagicMock()
    bad.status_code = 401
    bad.raise_for_status.side_effect = requests.exceptions.HTTPError("401")
    session.get.return_value = bad
    with pytest.raises(requests.exceptions.HTTPError):
        _client(session).fetch_announcements(date(2026, 7, 2), date(2026, 7, 3))
    assert session.get.call_count == 1
