"""Signal types emitted by strategies.

A Signal is an *intent*, not an order. It must pass through the RiskManager
(which may approve, resize, or reject it) before it becomes a pending order.
"""
from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"     # open / add to a long position
    SELL = "SELL"   # close / reduce a long position
    HOLD = "HOLD"   # no action


@dataclass(frozen=True)
class Signal:
    """A trading intent for one symbol on one bar.

    Stop/target are expressed as *fractions of the entry (fill) price*, not absolute
    levels — the strategy doesn't know the fill price (fills happen next bar). The
    Position computes absolute stop/target from the actual fill at entry time.

    - For BUY: size_hint = target fraction of equity (0..1); stop_pct/target_pct optional.
    - For SELL: an exit intent (size_hint/stop_pct/target_pct ignored).
    """
    symbol: str
    side: Side
    reason: str
    size_hint: float = 0.0              # target fraction of equity (BUY only)
    stop_pct: float | None = None    # fixed stop-loss as fraction below entry, e.g. 0.08 (BUY only)
    target_pct: float | None = None  # take-profit as fraction above entry (BUY only)
    trailing_stop_pct: float | None = None  # trailing stop as fraction below the running peak (BUY only)
    trailing_atr_mult: float | None = None  # trailing stop at k×ATR below the running peak (BUY only)
