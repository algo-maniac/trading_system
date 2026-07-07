"""TokenNotifierCore: window gating, missing-token nag, stop-and-confirm on renewal,
per-day dedup — all clock/token/calendar effects injected, no Redis or mail server."""
from datetime import datetime, time
from zoneinfo import ZoneInfo

from trading_system.agents.token_notifier.core import TokenNotifierCore

IST = ZoneInfo("Asia/Kolkata")
START = time(7, 0)
END = time(15, 30)


class Harness:
    def __init__(self, *, token=False, trading=True):
        self.sent: list[str] = []
        self.token = token
        self.trading = trading
        self.clock = datetime(2026, 7, 7, 8, 0, tzinfo=IST)  # in-window trading day

    def core(self) -> TokenNotifierCore:
        return TokenNotifierCore(
            has_token=lambda: self.token,
            send=lambda msg: self.sent.append(msg),
            now=lambda: self.clock,
            is_trading_day=lambda d: self.trading,
            start=START, end=END,
        )


def test_skips_non_trading_day():
    h = Harness(trading=False)
    assert h.core().tick() == "skip_non_trading_day"
    assert h.sent == []


def test_skips_before_and_after_window():
    h = Harness()
    core = h.core()
    h.clock = datetime(2026, 7, 7, 6, 59, tzinfo=IST)   # 1 min before start
    assert core.tick() == "skip_off_window"
    h.clock = datetime(2026, 7, 7, 15, 31, tzinfo=IST)  # 1 min after end
    assert core.tick() == "skip_off_window"
    assert h.sent == []


def test_missing_token_nags_every_tick_with_incrementing_count():
    h = Harness(token=False)
    core = h.core()
    assert core.tick() == "reminded"
    assert core.tick() == "reminded"
    assert len(h.sent) == 2
    assert "#1" in h.sent[0] and "#2" in h.sent[1]
    assert "trading auth" in h.sent[0]


def test_present_token_without_prior_nag_is_silent():
    h = Harness(token=True)
    core = h.core()
    assert core.tick() == "token_present"
    assert core.tick() == "token_present"
    assert h.sent == []          # renewed before the window opened → never bother the user


def test_renewal_after_nag_sends_one_confirmation_then_quiet():
    h = Harness(token=False)
    core = h.core()
    core.tick()                  # nag
    h.token = True               # operator runs `trading auth`
    assert core.tick() == "confirmed_renewed"
    assert core.tick() == "token_present"     # stays quiet afterwards
    confirmations = [m for m in h.sent if "confirmed" in m]
    assert len(confirmations) == 1


def test_state_resets_across_days():
    h = Harness(token=False)
    core = h.core()
    core.tick()                  # day 1 nag
    h.token = True
    core.tick()                  # day 1 confirmation
    # Next trading day, token missing again → nag count restarts at 1.
    h.clock = datetime(2026, 7, 8, 8, 0, tzinfo=IST)
    h.token = False
    assert core.tick() == "reminded"
    assert "#1" in h.sent[-1] and "2026-07-08" in h.sent[-1]
