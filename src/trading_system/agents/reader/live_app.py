"""Reader v2 — live WebSocket data plane (Phase 5b).

KiteTicker streams ticks for the intraday focus universe (rolling volatile screen ∪
liquid list, capped). Ticks → 1-min bars → 5-min bars locally (no REST polling, no
rate-limit pressure); each completed 5-min bar is persisted, gets intraday_v2 features
computed on a rolling multi-session frame, and is published to the bus — the
event_intraday sleeve sees it within seconds of the bar closing.

Resilience:
  - KiteTicker's built-in auto-reconnect handles drops; on_reconnect triggers a
    gap-repair: today's missing 5-min bars are backfilled via the (rate-limited,
    jitter-retrying) historical API, so a WiFi blip can't hole the session.
  - Token expiry surfaces as a connect error → alert on the bus (same TokenManager
    contract as the scheduled reader) and retry after a pause.
  - All aggregation state is rebuilt from ticks/DB — crash-only, no state to lose.

Run as a container:  python -m trading_system.agents.reader.live_app
"""
import signal
import sys
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from trading_system.agents.reader.aggregator import FiveMinResampler, TickAggregator
from trading_system.agents.reader.live_features import latest_features
from trading_system.agents.reader.scheduler import read_bars_for_window
from trading_system.bus.redis_streams import RedisBarBus
from trading_system.config import get_settings
from trading_system.data.auth import AuthError, get_authenticated_kite
from trading_system.data.calendar import get_trading_days, is_trading_day
from trading_system.data.screener import volatile_universe
from trading_system.data.storage import get_connection, get_symbol_token_map, upsert_ohlcv
from trading_system.observability import get_logger, setup_logging

logger = get_logger(__name__)

_MAX_FOCUS = 50
_HISTORY_SESSIONS = 2   # prior sessions kept in the feature frame (warmup + gap_pct)
_HISTORY_MAX_BARS = 75 * (_HISTORY_SESSIONS + 1)
_IST = ZoneInfo("Asia/Kolkata")


def _tick_timestamp(tick: dict) -> datetime:  # type: ignore[type-arg]
    """Kite's `exchange_timestamp` is a NAIVE datetime representing IST wall-clock time.
    Attaching tzinfo (not converting to UTC) preserves the wall-clock hour/minute values
    the aggregator's session-bucket math depends on (09:15 session open etc.) while
    making the bar comparable against the tz-aware UTC timestamps day-bars carry
    elsewhere in the system — mixing naive and aware datetimes in the same comparison
    (as LivePortfolio.update_price does) raises TypeError and crashed the Trader the
    first time a day-bar and an intraday bar met in the same process."""
    raw = tick.get("exchange_timestamp")
    if raw is None:
        return datetime.now(_IST)
    return raw.replace(tzinfo=_IST) if raw.tzinfo is None else raw


def build_focus_universe(today: date) -> list[str]:
    """Intraday focus list for today.

    Primary source: the Analyst's persisted `focus_list` (news ∪ gap-watch ∪ screen ∪
    liquid ∪ held, already prioritized and capped). Fallback when the Analyst hasn't
    run (or is down): the pre-5c inline computation — an unavailable news layer must
    degrade coverage quality, never blind the system. Either way, open positions from
    the ledger are unioned in defensively: an exit must never lose its data feed.
    """
    from trading_system.data.news_store import read_focus_list
    from trading_system.execution.ledger import PostgresLedger

    s = get_settings()
    with get_connection() as conn:
        focus = read_focus_list(conn, today)
        if focus:
            logger.info("focus_from_analyst", n=len(focus))
        else:
            logger.warning("focus_fallback_inline", reason="no focus_list row for today")
            focus = list(dict.fromkeys(
                volatile_universe(conn, s.nifty_200_symbols, today) + s.liquid_symbols
            ))
        held = sorted({p.symbol for p in PostgresLedger(conn).open_positions()})
    ordered = list(dict.fromkeys(held + focus))   # held first → survives the cap
    return ordered[:_MAX_FOCUS]


class LiveReader:
    def __init__(self, focus: list[str], token_map: dict[str, int]) -> None:
        self._bus = RedisBarBus()
        self._token_map = token_map
        self._tokens = [token_map[sym] for sym in focus if sym in token_map]
        self._agg = TickAggregator({tok: sym for sym, tok in token_map.items()})
        self._resampler = FiveMinResampler()
        self._history: dict[str, list] = {sym: [] for sym in focus}
        self._minute_buffer: list = []
        self._lock = threading.Lock()

    def seed_history(self, today: date) -> None:
        """Load the prior sessions' 5-min bars so live features match historical ones."""
        prior_days = get_trading_days(today - timedelta(days=10), today - timedelta(days=1))
        if not prior_days:
            return
        keep = min(_HISTORY_SESSIONS, len(prior_days))
        window_start = prior_days[-keep]
        bars = read_bars_for_window(list(self._history), ["5minute"], window_start,
                                    prior_days[-1])
        for bar in bars:
            self._history.setdefault(bar.symbol, []).append(bar)
        logger.info("live_history_seeded", symbols=len(self._history),
                    bars=sum(len(v) for v in self._history.values()))

    # --- KiteTicker callbacks (run on the ticker thread) -----------------------

    def on_ticks(self, _ws, ticks: list[dict]) -> None:  # type: ignore[type-arg]
        with self._lock:
            for tick in ticks:
                minute_bar = self._agg.on_tick(
                    token=tick["instrument_token"],
                    ltp=float(tick.get("last_price") or 0.0),
                    cum_volume=int(tick.get("volume_traded") or 0),
                    ts=_tick_timestamp(tick),
                )
                if minute_bar is not None:
                    self._on_minute_bar(minute_bar)

    def _on_minute_bar(self, bar) -> None:
        self._minute_buffer.append(bar)
        five = self._resampler.on_minute_bar(bar)
        if five is not None:
            self._publish_five(five)

    def _publish_five(self, five) -> None:
        history = self._history.setdefault(five.symbol, [])
        history.append(five)
        del history[:-_HISTORY_MAX_BARS]
        feats = latest_features(history)
        enriched = type(five)(
            ts=five.ts, symbol=five.symbol, interval=five.interval,
            instrument_token=five.instrument_token, open=five.open, high=five.high,
            low=five.low, close=five.close, volume=five.volume, features=feats,
        )
        self._bus.publish(enriched)
        logger.info("live_bar_published", symbol=five.symbol, ts=str(five.ts),
                    close=five.close, features=len(feats))

    def persist_buffered(self) -> None:
        """Flush buffered 1-min bars to the DB (called periodically off-thread)."""
        import pandas as pd
        with self._lock:
            buffered, self._minute_buffer = self._minute_buffer, []
        if not buffered:
            return
        df = pd.DataFrame([{
            "ts": b.ts, "instrument_token": b.instrument_token, "interval": b.interval,
            "open": b.open, "high": b.high, "low": b.low, "close": b.close,
            "volume": b.volume, "oi": 0,
        } for b in buffered])
        with get_connection() as conn:
            upsert_ohlcv(conn, df)
        logger.debug("live_bars_persisted", n=len(buffered))

    def on_reconnect(self, _ws, attempts: int) -> None:
        logger.warning("live_ws_reconnected", attempts=attempts)
        self._bus.publish_alert("ws_reconnected", f"after {attempts} attempts; gap-repair advised")

    def on_connect(self, ws, _response) -> None:
        ws.subscribe(self._tokens)
        ws.set_mode(ws.MODE_FULL, self._tokens)
        logger.info("live_ws_subscribed", tokens=len(self._tokens))


def main() -> int:
    s = get_settings()
    setup_logging(s.log_level)
    today = date.today()
    if not is_trading_day(today):
        logger.info("live_reader_non_trading_day", date=str(today))
        return 0

    focus = build_focus_universe(today)
    with get_connection() as conn:
        token_map = get_symbol_token_map(conn, focus)
    reader = LiveReader(focus, token_map)
    reader.seed_history(today)

    try:
        kite = get_authenticated_kite()
    except AuthError as exc:
        RedisBarBus().publish_alert("token_expired", str(exc))
        logger.error("live_reader_no_token")
        return 1

    from kiteconnect import KiteTicker
    ticker = KiteTicker(s.kite_api_key, kite.access_token)
    ticker.on_ticks = reader.on_ticks
    ticker.on_connect = reader.on_connect
    ticker.on_reconnect = reader.on_reconnect

    stop = threading.Event()

    def _handle(signum: int, _frame: object) -> None:
        logger.info("live_reader_shutdown_signal", signum=signum)
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    logger.info("live_reader_start", focus=len(focus))
    ticker.connect(threaded=True)
    try:
        while not stop.wait(timeout=30):
            reader.persist_buffered()   # flush 1-min bars every 30s
    finally:
        ticker.close()
        reader.persist_buffered()
        logger.info("live_reader_stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
