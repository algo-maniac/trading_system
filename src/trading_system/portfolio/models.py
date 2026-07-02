"""Portfolio data models: Position, Fill, Trade."""
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Fill:
    """A single executed transaction (one leg of a trade)."""
    ts: datetime
    symbol: str
    side: str            # "BUY" or "SELL"
    qty: int
    price: float         # fill price including slippage
    fees: float          # total Zerodha charges for this fill
    slippage: float      # slippage cost (price impact) for this fill


@dataclass
class Position:
    """An open long position in one symbol."""
    symbol: str
    qty: int
    avg_price: float          # average entry price (incl. slippage)
    entry_ts: datetime
    stop_price: float | None = None      # absolute, computed from fill at entry (ratcheted up if trailing)
    target_price: float | None = None
    trailing_stop_pct: float | None = None  # if set, stop_price trails the running peak by this fraction
    trailing_atr_mult: float | None = None  # if set, stop_price trails by k×atr_14 (takes precedence over pct)
    bars_held: int = 0
    entry_fees: float = 0.0                  # fees paid on entry (for round-trip pnl)
    entry_slippage: float = 0.0

    def market_value(self, price: float) -> float:
        return self.qty * price

    def unrealized_pnl(self, price: float) -> float:
        return self.qty * (price - self.avg_price)


@dataclass
class Trade:
    """A completed round-trip (entry + exit)."""
    symbol: str
    side: str            # "LONG"
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    qty: int
    fees: float          # total fees (entry + exit)
    slippage: float      # total slippage (entry + exit)
    pnl: float           # net of fees and slippage
    return_pct: float
    exit_reason: str
