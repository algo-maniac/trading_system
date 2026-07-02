"""Deterministic, point-in-time replay engine.

Guarantees:
1. Point-in-time correctness: at bar T, only data ≤ T is visible (no lookahead).
2. Determinism: identical stored data → identical event stream every run.
   No wall-clock, no RNG, stable symbol ordering, stable timestamp tie-breaking.

The engine loads bars from the DB (already stored + cleaned) and merges streams
across symbols using a min-heap keyed on (ts, symbol, stream_index).
Stream_index ensures Python never needs to compare iterator objects.
"""
import hashlib
import heapq
import json
from collections.abc import Iterator
from datetime import date

import pandas as pd
import psycopg

from trading_system.data.storage import get_connection, read_features, read_ohlcv
from trading_system.features.registry import DEFAULT_FEATURE_SET
from trading_system.observability import get_logger
from trading_system.replay.events import BarEvent

logger = get_logger(__name__)


def _load_bars(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    symbol: str,
    instrument_token: int,
    interval: str,
    from_date: date,
    to_date: date,
    feature_set_version: str,
) -> list[BarEvent]:
    ohlcv = read_ohlcv(conn, instrument_token, interval, from_date, to_date)
    if ohlcv.empty:
        return []

    # Build a ts → features lookup
    feat_df = read_features(conn, instrument_token, interval, feature_set_version, from_date, to_date)
    feat_map: dict[pd.Timestamp, dict[str, float]] = {}
    if not feat_df.empty:
        for _, row in feat_df.iterrows():
            feat_map[row["ts"]] = {
                col: float(row[col])
                for col in feat_df.columns
                if col != "ts" and pd.notna(row[col])
            }

    bars: list[BarEvent] = []
    for _, row in ohlcv.iterrows():
        bars.append(BarEvent(
            ts=row["ts"].to_pydatetime(),
            symbol=symbol,
            interval=interval,
            instrument_token=int(instrument_token),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=int(row["volume"]),
            features=feat_map.get(row["ts"], {}),
        ))
    return sorted(bars)  # sorted by (ts, symbol) via dataclass order=True


class ReplayEngine:
    """Merge bar streams across symbols into a single deterministic event sequence."""

    def __init__(
        self,
        symbols: list[str],
        token_map: dict[str, int],
        interval: str,
        from_date: date,
        to_date: date,
        feature_set_version: str = DEFAULT_FEATURE_SET,
    ) -> None:
        self._symbols = sorted(symbols)  # deterministic stream ordering
        self._token_map = token_map
        self._interval = interval
        self._from_date = from_date
        self._to_date = to_date
        self._feature_set_version = feature_set_version

    def run(self) -> Iterator[BarEvent]:
        """Yield BarEvents in ascending (ts, symbol) order."""
        with get_connection() as conn:
            # Load per-symbol bar lists then heap-merge
            iters = []
            for i, symbol in enumerate(self._symbols):
                token = self._token_map.get(symbol)
                if not token:
                    logger.warning("replay_skip_no_token", symbol=symbol)
                    continue
                bars = _load_bars(
                    conn, symbol, token, self._interval,
                    self._from_date, self._to_date, self._feature_set_version,
                )
                if bars:
                    iters.append((i, iter(bars)))

            # heap element: (ts, symbol, stream_index, bar, iterator)
            # stream_index breaks ties after (ts, symbol) so we never compare bars or iterators
            heap: list[tuple] = []
            for stream_idx, it in iters:
                try:
                    bar = next(it)
                    heapq.heappush(heap, (bar.ts, bar.symbol, stream_idx, bar, it))
                except StopIteration:
                    pass

            while heap:
                _ts, _sym, _idx, bar, it = heapq.heappop(heap)
                yield bar
                try:
                    nxt = next(it)
                    heapq.heappush(heap, (nxt.ts, nxt.symbol, _idx, nxt, it))
                except StopIteration:
                    pass

    def compute_hash(self) -> str:
        """SHA-256 of the full event stream — used to verify determinism.

        Two runs with identical DB state must return the same hex digest.
        """
        h = hashlib.sha256()
        for bar in self.run():
            payload = json.dumps({
                "ts":       bar.ts.isoformat(),
                "symbol":   bar.symbol,
                "interval": bar.interval,
                "open":     bar.open,
                "high":     bar.high,
                "low":      bar.low,
                "close":    bar.close,
                "volume":   bar.volume,
            }, sort_keys=True)
            h.update(payload.encode())
        return h.hexdigest()
