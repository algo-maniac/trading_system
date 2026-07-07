"""TokenNotifierCore: decide, per tick, whether to send a token-renewal reminder.

All effects (token presence check, message send, clock, calendar) are injected, so the
whole decision flow — window gating, missing-token nag, stop-and-confirm on renewal, and
per-day dedup — is unit-testable without Redis, a mail server, or real time. The `send`
callable is the only channel seam (email today; swappable without touching this logic).

Contract per tick (the scheduler fires this every ~20 min):
  - not a trading day, or outside [start, end] IST  → do nothing
  - today's token MISSING                           → send a nag (every tick in-window)
  - token PRESENT and we'd nagged today              → send ONE "renewed" confirmation
  - token PRESENT and we hadn't nagged               → do nothing (no spam)
State resets when the IST date rolls over, so each morning starts clean.
"""
from collections.abc import Callable
from datetime import date, datetime, time


class TokenNotifierCore:
    def __init__(
        self,
        has_token: Callable[[], bool],
        send: Callable[[str], object],
        now: Callable[[], datetime],
        is_trading_day: Callable[[date], bool],
        start: time,
        end: time,
    ) -> None:
        self._has_token = has_token
        self._send = send
        self._now = now
        self._is_trading_day = is_trading_day
        self._start = start
        self._end = end
        self._day: date | None = None
        self._reminders = 0
        self._confirmed = False

    def _roll_day(self, today: date) -> None:
        if today != self._day:
            self._day = today
            self._reminders = 0
            self._confirmed = False

    def tick(self) -> str:
        now = self._now()
        today = now.date()
        self._roll_day(today)

        if not self._is_trading_day(today):
            return "skip_non_trading_day"
        if not (self._start <= now.time() <= self._end):
            return "skip_off_window"

        if self._has_token():
            # Renewed: confirm once (only if we actually nagged), then stay quiet.
            if self._reminders > 0 and not self._confirmed:
                self._confirmed = True
                self._send(
                    f"✅ Kite access token for {today} detected — renewal "
                    "confirmed, reminders stopping for today."
                )
                return "confirmed_renewed"
            return "token_present"

        self._reminders += 1
        self._send(
            f"⚠️ No Kite access token for {today} yet (reminder "
            f"#{self._reminders}). Run `trading auth` before the 09:15 IST open — "
            "live data & paper trading stay paused until it's renewed."
        )
        return "reminded"
