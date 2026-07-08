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
import time
from collections import deque
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
# KiteTicker bakes the access token in at construction and reuses it for every internal
# auto-reconnect. Two failure shapes need a ticker rebuild (drop it, re-auth, reconnect):
#   1. NEVER connects — token rotated at midnight IST → the WS 403s forever and on_connect
#      never fires to reset KiteTicker's `attempts`, so `attempts` climbs unbounded.
#   2. FLAPS — connects, streams a burst, drops (403), reconnects, repeat. Each on_connect
#      resets `attempts`, so trigger (1) never fires; but a healthy link doesn't reconnect
#      every minute, so a burst of reconnects in a short window is itself the signal.
# (1) is caught by the consecutive-attempts threshold; (2) by a rolling-window count.
_REBUILD_AFTER_RECONNECTS = 8          # (1) consecutive failed attempts, no on_connect
_FLAP_WINDOW_SECONDS = 180.0           # (2) rolling window for reconnect-rate
_FLAP_MAX_RECONNECTS = 5               # (2) this many reconnects within the window = flapping


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


def _assemble_focus(focus: list[str], held: list[str], fallback: list[str],
                    cap: int = _MAX_FOCUS) -> tuple[list[str], bool]:
    """Held-first union, capped; drop to the static `fallback` when `focus` is empty.

    Held positions go first so an open name always survives the cap (its exit must
    keep a data feed). Returns (ordered_symbols, used_fallback)."""
    used_fallback = not focus
    src = list(focus) if focus else list(fallback)
    ordered = list(dict.fromkeys(list(held) + src))
    return ordered[:cap], used_fallback


def _screen_fallback(conn: object, s: object, today: date) -> list[str]:
    """The computed middle-tier fallback (live volatile screen ∪ liquid anchors),
    guarded so a screen/DB error degrades to [] (→ static fallback) instead of raising."""
    try:
        screen = volatile_universe(conn, s.nifty_200_symbols, today)  # type: ignore[attr-defined]
        if not screen:
            # An empty screen must fall through to the richer STATIC fallback (frozen
            # movers ∪ liquid), not collapse to bare liquid anchors (13 large-caps,
            # a weak intraday universe) — that thin case is what we're guarding against.
            return []
        return list(dict.fromkeys(screen + list(s.liquid_symbols)))   # type: ignore[attr-defined]
    except Exception as exc:
        logger.error("focus_screen_failed", error=str(exc))
        return []


def build_focus_universe(today: date) -> list[str]:
    """Intraday focus list for today — NEVER empty, NEVER raises.

    Degrades through three tiers so the intraday sleeve always has a sane universe:
      1. Analyst's persisted `focus_list` (news ∪ gap-watch ∪ screen ∪ liquid ∪ held);
      2. else the live volatile screen ∪ liquid anchors (computed, DB-dependent);
      3. else the STATIC `intraday_fallback_symbols` constant — valid with zero DB
         dependency, so a lapsed token / empty screen / DB outage can't blind the
         reader (this was the gap the un-renewed-token morning exposed).
    Open positions from the ledger are unioned in first (an exit must keep its feed).
    Any DB failure is swallowed and we still return the static fallback + alert.
    """
    from trading_system.data.news_store import read_focus_list
    from trading_system.execution.ledger import PostgresLedger

    s = get_settings()
    focus: list[str] = []
    held: list[str] = []
    try:
        with get_connection() as conn:
            # Autocommit BEFORE any query: PostgresLedger's constructor flips
            # autocommit=True, which psycopg refuses once a plain SELECT has already
            # opened a transaction on this connection (ProgrammingError: connection
            # in transaction status INTRANS) — caught live: this silently dropped
            # held-position resolution every cycle (masked by the outer except).
            conn.autocommit = True
            focus = read_focus_list(conn, today)
            if focus:
                logger.info("focus_from_analyst", n=len(focus))
            else:
                logger.warning("focus_fallback_inline", reason="no focus_list row for today")
                focus = _screen_fallback(conn, s, today)
            held = sorted({p.symbol for p in PostgresLedger(conn).open_positions()})
    except Exception as exc:
        # Total DB failure — keep going; the static fallback needs no DB.
        logger.error("focus_build_db_failed", error=str(exc))

    ordered, used_fallback = _assemble_focus(focus, held, s.intraday_fallback_symbols)
    if used_fallback:
        logger.warning("focus_static_fallback", n=len(ordered))
        try:
            RedisBarBus().publish_alert(
                "focus_static_fallback",
                f"focus_list + live screen unavailable; streaming {len(ordered)} "
                "static fallback symbols",
            )
        except Exception as exc:
            logger.error("focus_fallback_alert_failed", error=str(exc))
    return ordered


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
        # Set by on_reconnect (ticker thread) when reconnects are exhausted OR flapping;
        # the session supervisor (main thread) polls it and tears the ticker down.
        self._rebuild_needed = threading.Event()
        self._reconnect_times: deque[float] = deque()   # monotonic ts of recent reconnects
        self._clock = time.monotonic                    # injectable for tests

    def needs_rebuild(self) -> bool:
        return self._rebuild_needed.is_set()

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
        # Record this reconnect and prune anything older than the flap window.
        now = self._clock()
        self._reconnect_times.append(now)
        cutoff = now - _FLAP_WINDOW_SECONDS
        while self._reconnect_times and self._reconnect_times[0] < cutoff:
            self._reconnect_times.popleft()
        # Rebuild if it CAN'T connect (attempts climb, on_connect never resets them) OR if
        # it keeps flapping (many reconnects in the window even though on_connect resets
        # `attempts` each time — the case a plain consecutive-count check misses).
        flapping = len(self._reconnect_times) >= _FLAP_MAX_RECONNECTS
        if attempts >= _REBUILD_AFTER_RECONNECTS or flapping:
            logger.error("live_ws_reconnect_exhausted", attempts=attempts,
                         window_reconnects=len(self._reconnect_times), flapping=flapping)
            self._rebuild_needed.set()

    def on_connect(self, ws, _response) -> None:
        ws.subscribe(self._tokens)
        ws.set_mode(ws.MODE_FULL, self._tokens)
        logger.info("live_ws_subscribed", tokens=len(self._tokens))


# When idle (non-trading day / awaiting a token), re-check this often instead of
# exiting — exiting under compose's `restart: unless-stopped` is a busy restart loop.
_IDLE_RECHECK_SECONDS = 900.0


def _ist_today() -> date:
    """Trading-session date in IST — NOT date.today(), which returns the container's
    UTC date and would call a late-evening IST session (still the same trading day)
    a different, possibly non-trading, day."""
    return datetime.now(_IST).date()


def _run_session(s: object, kite: object, today: date, stop: threading.Event) -> None:
    """Stream one trading day's ticks until shutdown or the IST day rolls over."""
    focus = build_focus_universe(today)
    with get_connection() as conn:
        token_map = get_symbol_token_map(conn, focus)
    reader = LiveReader(focus, token_map)
    reader.seed_history(today)

    from kiteconnect import KiteTicker
    ticker = KiteTicker(s.kite_api_key, kite.access_token)  # type: ignore[attr-defined]
    ticker.on_ticks = reader.on_ticks
    ticker.on_connect = reader.on_connect
    ticker.on_reconnect = reader.on_reconnect

    logger.info("live_reader_start", focus=len(focus))
    ticker.connect(threaded=True)
    try:
        while not stop.wait(timeout=30):
            reader.persist_buffered()   # flush 1-min bars every 30s
            if reader.needs_rebuild():
                # Ticker can't reconnect with the current token (rotation). Return to
                # the daemon loop, which re-reads today's token from Redis and rebuilds.
                logger.warning("live_reader_ticker_rebuild",
                               reason="reconnect attempts exhausted; re-authenticating")
                break
            if _ist_today() != today:   # midnight rollover → re-evaluate the day
                logger.info("live_reader_day_rollover", was=str(today))
                break
    finally:
        ticker.close()
        reader.persist_buffered()
        logger.info("live_reader_session_ended", date=str(today))


def _daemon_loop(s: object, stop: threading.Event) -> None:
    """Resilient daemon: NEVER exit on a transient "can't run yet" condition — under
    compose's `restart: unless-stopped`, exiting (0 on a weekend, 1 on a missing
    morning token) becomes a tight restart loop (seen: 150+ restarts over a weekend).
    Instead sleep and re-check so the same process spans non-trading days and the
    daily `trading auth` gap, and streams once each session opens."""
    logger.info("live_reader_daemon_start")
    while not stop.is_set():
        today = _ist_today()
        if not is_trading_day(today):
            logger.info("live_reader_non_trading_day", date=str(today))
            stop.wait(timeout=_IDLE_RECHECK_SECONDS)
            continue
        try:
            kite = get_authenticated_kite()
        except AuthError as exc:
            RedisBarBus().publish_alert("token_expired", str(exc))
            logger.warning("live_reader_awaiting_token", detail=str(exc))
            stop.wait(timeout=s.reader_token_poll_seconds)  # type: ignore[attr-defined]
            continue
        try:
            _run_session(s, kite, today, stop)
        except AuthError as exc:
            RedisBarBus().publish_alert("token_expired", str(exc))
            logger.warning("live_reader_token_lost_mid_session", detail=str(exc))
            stop.wait(timeout=s.reader_token_poll_seconds)  # type: ignore[attr-defined]
        except Exception as exc:
            # A single bad session (network, Kite blip) must not kill the daemon.
            logger.error("live_reader_session_failed", error=str(exc), exc_info=True)
            stop.wait(timeout=60)
    logger.info("live_reader_stopped")


def main() -> int:
    s = get_settings()
    setup_logging(s.log_level)

    stop = threading.Event()

    def _handle(signum: int, _frame: object) -> None:
        logger.info("live_reader_shutdown_signal", signum=signum)
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    _daemon_loop(s, stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
