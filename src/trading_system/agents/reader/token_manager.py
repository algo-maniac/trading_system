"""Kite token self-heal.

Kite access tokens expire daily at midnight IST and require an interactive login to refresh.
Per the chosen policy (no stored TOTP), the Reader does NOT auto-login: when the token is
missing/expired it raises a `token_expired` alert on the bus + logs, and pauses ingestion. The
operator runs `trading auth`; the Reader auto-resumes once a fresh token reappears in Redis.

This wrapper turns auth failures into alerts-and-pauses instead of crashes.
"""
from collections.abc import Callable
from typing import TypeVar

from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException

from trading_system.data.auth import AuthError, get_access_token, get_authenticated_kite
from trading_system.observability import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class TokenManager:
    """Provides authenticated Kite clients and converts token errors into bus alerts."""

    def __init__(self, alert_bus: object | None = None) -> None:
        # alert_bus is any object with publish_alert(kind, detail); None disables alerting.
        self._alert = alert_bus

    def _alert_expired(self, detail: str) -> None:
        logger.warning("token_expired", detail=detail)
        if self._alert is not None:
            self._alert.publish_alert("token_expired", detail)  # type: ignore[attr-defined]

    def has_token(self) -> bool:
        return get_access_token() is not None

    def get_kite(self) -> KiteConnect | None:
        """Return an authenticated client, or None (after alerting) if today's token is absent."""
        try:
            return get_authenticated_kite()
        except AuthError as exc:
            self._alert_expired(str(exc))
            return None

    def guard(self, fn: Callable[[], T]) -> T | None:
        """Run `fn()`; if it fails with a token/auth error, alert + return None (caller pauses).
        Other exceptions propagate — they are real bugs, not an expected token expiry."""
        try:
            return fn()
        except (TokenException, AuthError) as exc:
            self._alert_expired(str(exc))
            return None
