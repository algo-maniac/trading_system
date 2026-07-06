"""Live intraday feature computation for streamed 5-minute bars.

Reuses the exact batch `compute_intraday_features` on a rolling frame of
(prior sessions' bars from the DB) + (today's streamed bars), then returns the LAST
row's features — so a live bar carries the same feature values a later historical
rebuild would produce. Prior-session bars matter twice:
  - gap_pct needs the prior session's last close;
  - the continuous indicators (atr/rsi/ema/adx) carry across sessions in the backtest
    frame; feeding today's bars alone would leave them in warmup all morning and the
    live features would silently diverge from the validated ones.
"""
import math
from datetime import UTC

import pandas as pd

from trading_system.features.engineering import compute_intraday_features
from trading_system.features.registry import FEATURE_SETS
from trading_system.replay.events import BarEvent

_FEATURE_NAMES = FEATURE_SETS["intraday_v2"].feature_names


def bars_to_frame(bars: list[BarEvent]) -> pd.DataFrame:
    """Build the OHLCV frame, normalizing every ts to UTC.

    DB-seeded history bars carry UTC tzinfo (psycopg's TIMESTAMPTZ read); live
    ticker-aggregated bars carry IST tzinfo (live_app._tick_timestamp attaches it
    deliberately, to preserve wall-clock hour/minute for the aggregator's session
    buckets). Mixing tz-aware datetimes with different UTC offsets in one pandas
    column makes `pd.to_datetime` raise ("Tz-aware datetime.datetime cannot be
    converted to datetime64 unless utc=True") — caught live: this crashed on_ticks
    on nearly every published bar and forced a WebSocket reconnect each time.
    Converting to one common offset here preserves the absolute instant (and the
    session-grouping calendar date, since NSE's 09:15-15:30 IST session never
    crosses a UTC midnight — see data/storage.py:_ist_day_bounds) while avoiding it.
    """
    return pd.DataFrame({
        "ts": [b.ts.astimezone(UTC) if b.ts.tzinfo else b.ts for b in bars],
        "open": [b.open for b in bars],
        "high": [b.high for b in bars],
        "low": [b.low for b in bars],
        "close": [b.close for b in bars],
        "volume": [b.volume for b in bars],
    })


def latest_features(history_and_today: list[BarEvent]) -> dict[str, float]:
    """intraday_v2 features for the most recent bar of the combined frame.

    NaNs (warmup, missing prior session) are dropped — consumers treat a missing key
    as "feature unavailable", the same contract as the DB feature store.
    """
    if not history_and_today:
        return {}
    out = compute_intraday_features(bars_to_frame(history_and_today))
    last = out.iloc[-1]
    feats: dict[str, float] = {}
    for name in _FEATURE_NAMES:
        if name in out.columns:
            value = float(last[name])
            if not math.isnan(value):
                feats[name] = value
    return feats
