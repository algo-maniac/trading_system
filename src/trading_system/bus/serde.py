"""BarEvent <-> Redis-Stream-field-map (flat str->str) serialization.

Redis Stream entries are flat string maps, so each BarEvent field is encoded as a string and
the features dict is JSON. Round-trip identity is a tested invariant — the bar a consumer
reconstructs must equal the bar the producer published (so the forward-test path is exact).
"""
import json
from datetime import datetime

from trading_system.replay.events import BarEvent


def bar_to_fields(bar: BarEvent) -> dict[str, str]:
    """Encode a BarEvent as a flat string map for XADD."""
    return {
        "ts": bar.ts.isoformat(),
        "symbol": bar.symbol,
        "interval": bar.interval,
        "instrument_token": str(bar.instrument_token),
        "open": repr(bar.open),
        "high": repr(bar.high),
        "low": repr(bar.low),
        "close": repr(bar.close),
        "volume": str(bar.volume),
        "features": json.dumps(bar.features, sort_keys=True),
    }


def fields_to_bar(fields: dict[str, str]) -> BarEvent:
    """Reconstruct a BarEvent from a stream field map. Inverse of `bar_to_fields`."""
    return BarEvent(
        ts=datetime.fromisoformat(fields["ts"]),
        symbol=fields["symbol"],
        interval=fields["interval"],
        instrument_token=int(fields["instrument_token"]),
        open=float(fields["open"]),
        high=float(fields["high"]),
        low=float(fields["low"]),
        close=float(fields["close"]),
        volume=int(fields["volume"]),
        features=json.loads(fields.get("features", "{}")),
    )
