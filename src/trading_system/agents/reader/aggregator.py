"""Tick → bar aggregation for the live (WebSocket) data plane.

Kite's ticker pushes ticks (last price + cumulative day volume). The aggregator rolls
them into 1-minute OHLCV bars locally — this is what makes real-time operation free of
REST rate limits: the stream pushes, we never poll.

Mechanics:
  - A tick belongs to the minute bucket `ts.replace(second=0)`. The first tick at or
    past a new minute CLOSES the previous minute's bar (returned to the caller) and
    opens the next. `flush()` force-closes whatever is open (session end / shutdown).
  - Volume: Kite reports volume as the day's CUMULATIVE traded quantity, so a bar's
    volume is the delta between the last cumulative seen in this bar and the last seen
    before it. First bar of the session takes the full cumulative (open-auction volume
    belongs to the first bar; same convention as Kite's own historical 1-min candles).
  - `resample_5min` folds completed 1-min bars into NSE-grid 5-minute bars
    (09:15, 09:20, ... — bucket = minutes-since-09:15 floored to 5).

Pure and synchronous — the WebSocket callback feeds it, tests feed it directly.
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from trading_system.replay.events import BarEvent

_SESSION_OPEN = time(9, 15)


@dataclass
class _Building:
    minute: datetime
    open: float
    high: float
    low: float
    close: float
    last_cum_volume: int
    start_cum_volume: int   # cumulative volume BEFORE this bar's first tick


class TickAggregator:
    def __init__(self, token_to_symbol: dict[int, str]) -> None:
        self._symbols = token_to_symbol
        self._building: dict[int, _Building] = {}
        self._prev_cum_volume: dict[int, int] = {}

    def on_tick(self, token: int, ltp: float, cum_volume: int, ts: datetime) -> BarEvent | None:
        """Feed one tick. Returns the COMPLETED prior 1-min bar when a minute rolls over."""
        symbol = self._symbols.get(token)
        if symbol is None or ltp <= 0:
            return None
        minute = ts.replace(second=0, microsecond=0)
        b = self._building.get(token)
        completed: BarEvent | None = None
        if b is not None and minute > b.minute:
            completed = self._to_bar(token, b)
            b = None
        if b is None:
            self._building[token] = _Building(
                minute=minute, open=ltp, high=ltp, low=ltp, close=ltp,
                last_cum_volume=cum_volume,
                start_cum_volume=self._prev_cum_volume.get(token, 0),
            )
        else:
            b.high = max(b.high, ltp)
            b.low = min(b.low, ltp)
            b.close = ltp
            b.last_cum_volume = max(b.last_cum_volume, cum_volume)
        return completed

    def flush(self) -> list[BarEvent]:
        """Force-close all in-progress bars (call at session end / shutdown)."""
        out = [self._to_bar(token, b) for token, b in sorted(self._building.items())]
        self._building.clear()
        return out

    def _to_bar(self, token: int, b: _Building) -> BarEvent:
        volume = max(0, b.last_cum_volume - b.start_cum_volume)
        self._prev_cum_volume[token] = b.last_cum_volume
        return BarEvent(
            ts=b.minute, symbol=self._symbols[token], interval="minute",
            instrument_token=token, open=b.open, high=b.high, low=b.low, close=b.close,
            volume=volume, features={},
        )


def five_min_bucket(ts: datetime) -> datetime:
    """The NSE 5-min grid bucket a 1-min bar belongs to (09:15, 09:20, ...)."""
    session_open = ts.replace(hour=_SESSION_OPEN.hour, minute=_SESSION_OPEN.minute,
                              second=0, microsecond=0)
    offset = int((ts - session_open).total_seconds() // 60)
    return session_open + timedelta(minutes=(offset // 5) * 5)


class FiveMinResampler:
    """Fold completed 1-min bars into 5-min bars on the NSE grid, per symbol."""

    def __init__(self) -> None:
        self._building: dict[str, BarEvent] = {}

    def on_minute_bar(self, bar: BarEvent) -> BarEvent | None:
        """Feed a completed 1-min bar; returns the COMPLETED prior 5-min bar on rollover."""
        bucket = five_min_bucket(bar.ts)
        b = self._building.get(bar.symbol)
        completed: BarEvent | None = None
        if b is not None and bucket > b.ts:
            completed = b
            b = None
        if b is None:
            self._building[bar.symbol] = BarEvent(
                ts=bucket, symbol=bar.symbol, interval="5minute",
                instrument_token=bar.instrument_token, open=bar.open, high=bar.high,
                low=bar.low, close=bar.close, volume=bar.volume, features={},
            )
        else:
            self._building[bar.symbol] = BarEvent(
                ts=b.ts, symbol=b.symbol, interval="5minute",
                instrument_token=b.instrument_token, open=b.open,
                high=max(b.high, bar.high), low=min(b.low, bar.low), close=bar.close,
                volume=b.volume + bar.volume, features={},
            )
        return completed

    def flush(self) -> list[BarEvent]:
        out = sorted(self._building.values(), key=lambda b: (b.ts, b.symbol))
        self._building.clear()
        return out
