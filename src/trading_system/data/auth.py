"""Zerodha Kite Connect authentication.

Access tokens expire every day at midnight IST. Operators must run `trading auth`
each morning before any command that talks to Kite. The token is cached in Redis
so all other modules can call get_authenticated_kite() without re-prompting.
"""
from datetime import date
from typing import Optional

import redis
from kiteconnect import KiteConnect

from trading_system.config import get_settings
from trading_system.observability import get_logger

logger = get_logger(__name__)

_REDIS_KEY = "kite:access_token:{date}"
_TOKEN_TTL = 86_400  # 24 h (Kite expires it at midnight IST anyway)


class AuthError(Exception):
    """Raised when no valid access token exists for today."""


def _redis() -> redis.Redis:  # type: ignore[type-arg]
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


def get_login_url() -> str:
    kite = KiteConnect(api_key=get_settings().kite_api_key)
    url: str = kite.login_url()
    logger.info("kite_login_url", url=url)
    return url


def complete_login(request_token: str) -> str:
    """Exchange request_token for an access_token and cache it in Redis."""
    s = get_settings()
    kite = KiteConnect(api_key=s.kite_api_key)
    data = kite.generate_session(request_token, api_secret=s.kite_api_secret)
    access_token: str = data["access_token"]

    key = _REDIS_KEY.format(date=date.today().isoformat())
    _redis().setex(key, _TOKEN_TTL, access_token)
    logger.info("kite_session_created", user_id=data.get("user_id"))
    return access_token


def get_access_token() -> Optional[str]:
    key = _REDIS_KEY.format(date=date.today().isoformat())
    return _redis().get(key)  # type: ignore[return-value]


def get_authenticated_kite() -> KiteConnect:
    """Return a ready KiteConnect client with today's access token set.

    Raises AuthError if today's token is missing — operator must run `trading auth`.
    """
    token = get_access_token()
    if not token:
        raise AuthError(
            "No Kite access token found for today.\n"
            "Run:  trading auth\n"
            "Note: Kite access tokens expire daily and must be refreshed each morning."
        )
    kite = KiteConnect(api_key=get_settings().kite_api_key)
    kite.set_access_token(token)
    return kite
