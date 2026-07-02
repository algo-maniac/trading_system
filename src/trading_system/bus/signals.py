"""SignalEvent: a strategy's trade intent on the bus, plus its serde.

Produced by the Strategist agent, consumed by the Trader (and later the Analyst).
The id is a deterministic hash of (sleeve, strategy, symbol, ts, side) — under
at-least-once delivery every consumer dedupes on it, and the journal keys orders
by it, so a redelivered event can never double-trade.
"""
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class SignalEvent:
    id: str
    ts: datetime
    sleeve: str
    strategy: str
    symbol: str
    side: str                       # "BUY" | "SELL"
    size_hint: float | None = None
    stop_pct: float | None = None
    target_pct: float | None = None
    trailing_stop_pct: float | None = None
    trailing_atr_mult: float | None = None
    reason: str = ""
    features: dict = field(default_factory=dict)  # type: ignore[type-arg]
    ref_price: float = 0.0          # signal bar's close — the risk gate's sizing price


def signal_id(sleeve: str, strategy: str, symbol: str, ts: datetime, side: str) -> str:
    key = f"{sleeve}|{strategy}|{symbol}|{ts.isoformat()}|{side}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


_OPT_FLOATS = ("size_hint", "stop_pct", "target_pct", "trailing_stop_pct", "trailing_atr_mult")


def signal_to_fields(sig: SignalEvent) -> dict[str, str]:
    fields = {
        "id": sig.id,
        "ts": sig.ts.isoformat(),
        "sleeve": sig.sleeve,
        "strategy": sig.strategy,
        "symbol": sig.symbol,
        "side": sig.side,
        "reason": sig.reason,
        "features": json.dumps(sig.features, sort_keys=True),
        "ref_price": repr(sig.ref_price),
    }
    for name in _OPT_FLOATS:
        value = getattr(sig, name)
        if value is not None:
            fields[name] = repr(value)
    return fields


def fields_to_signal(fields: dict[str, str]) -> SignalEvent:
    def opt(name: str) -> float | None:
        return float(fields[name]) if name in fields else None

    return SignalEvent(
        id=fields["id"],
        ts=datetime.fromisoformat(fields["ts"]),
        sleeve=fields["sleeve"],
        strategy=fields["strategy"],
        symbol=fields["symbol"],
        side=fields["side"],
        size_hint=opt("size_hint"),
        stop_pct=opt("stop_pct"),
        target_pct=opt("target_pct"),
        trailing_stop_pct=opt("trailing_stop_pct"),
        trailing_atr_mult=opt("trailing_atr_mult"),
        reason=fields.get("reason", ""),
        features=json.loads(fields.get("features", "{}")),
        ref_price=float(fields.get("ref_price", "0.0")),
    )
