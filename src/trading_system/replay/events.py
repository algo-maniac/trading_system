"""Event types emitted by the replay engine."""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, order=True)
class BarEvent:
    """A single OHLCV bar for one symbol, emitted by the replay engine.

    Ordering: by (ts, symbol) — deterministic tie-breaking when two symbols
    share the same timestamp (e.g. two day bars on the same date).

    features dict is excluded from ordering and hashing (it's supplementary
    data; the bar identity is defined by ts/symbol/interval/OHLCV).
    """
    ts: datetime
    symbol: str
    interval: str
    instrument_token: int
    open: float
    high: float
    low: float
    close: float
    volume: int
    features: dict[str, float] = field(default_factory=dict, compare=False, hash=False)
