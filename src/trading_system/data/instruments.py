"""Instrument master: sync from Kite and resolve NIFTY 50 tokens."""
import psycopg
from kiteconnect import KiteConnect

from trading_system.config import get_settings
from trading_system.data.storage import get_symbol_token_map, upsert_instruments
from trading_system.observability import get_logger

logger = get_logger(__name__)

_EXCHANGE = "NSE"


def sync_instruments(kite: KiteConnect, conn: psycopg.Connection) -> int:  # type: ignore[type-arg]
    """Pull the full NSE instrument dump from Kite and upsert into the DB."""
    logger.info("instrument_sync_start", exchange=_EXCHANGE)
    raw = kite.instruments(_EXCHANGE)
    instruments = [
        {
            "instrument_token": int(i["instrument_token"]),
            "tradingsymbol":    str(i["tradingsymbol"]),
            "exchange":         str(i.get("exchange", _EXCHANGE)),
            "segment":          str(i.get("segment", "")),
            "name":             str(i.get("name", "") or ""),
            "lot_size":         int(i.get("lot_size", 1) or 1),
            "tick_size":        float(i.get("tick_size", 0.05) or 0.05),
            "instrument_type":  str(i.get("instrument_type", "EQ") or "EQ"),
        }
        for i in raw
    ]
    count = upsert_instruments(conn, instruments)
    logger.info("instrument_sync_done", exchange=_EXCHANGE, count=count)
    return count


def resolve_nifty50_tokens(conn: psycopg.Connection) -> dict[str, int]:  # type: ignore[type-arg]
    """Return {tradingsymbol: instrument_token} for all configured NIFTY 50 symbols."""
    symbols = get_settings().nifty_50_symbols
    token_map = get_symbol_token_map(conn, symbols)
    missing = sorted(set(symbols) - set(token_map))
    if missing:
        logger.warning(
            "nifty50_tokens_missing",
            missing=missing,
            hint="Run `trading sync-instruments` first, or check symbol spelling.",
        )
    logger.info("nifty50_resolved", found=len(token_map), missing=len(missing))
    return token_map
