"""LivePortfolio.update_price: must not crash when bars from different sources carry
mixed naive/aware timestamps — this took down the Trader live the first time a
tz-aware day-bar (from Postgres) and a naive intraday bar met in the same process."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from tests.fakes import FakeLedger
from trading_system.portfolio.live import LivePortfolio

IST = ZoneInfo("Asia/Kolkata")


def test_update_price_accepts_naive_timestamp_after_aware_one():
    view = LivePortfolio(FakeLedger(), "momentum")
    view.update_price("RELIANCE", 100.0, datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc))
    # a naive ts (e.g. an un-normalized intraday source) must not crash the comparison
    view.update_price("RELIANCE", 101.0, datetime(2026, 7, 3, 18, 0))
    assert view._last_price["RELIANCE"] == 101.0


def test_update_price_accepts_naive_timestamp_before_aware_one():
    view = LivePortfolio(FakeLedger(), "momentum")
    view.update_price("RELIANCE", 100.0, datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc))
    view.update_price("RELIANCE", 99.0, datetime(2026, 7, 3, 6, 0))  # naive, earlier
    # still tracked as the latest PRICE even though it's not the latest "now" —
    # update_price always records the price; only self._now advances monotonically
    assert view._last_price["RELIANCE"] == 99.0


def test_update_price_handles_mixed_aware_timezones():
    view = LivePortfolio(FakeLedger(), "momentum")
    view.update_price("RELIANCE", 100.0, datetime(2026, 7, 3, 17, 0, tzinfo=IST))
    view.update_price("RELIANCE", 101.0, datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc))
    assert view._last_price["RELIANCE"] == 101.0
