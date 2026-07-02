"""Strategy abstract base class.

A Strategy is a pure decision function: given the current bar (with point-in-time
features) and a read-only view of the portfolio, it emits zero or more Signals.
Strategies NEVER place orders directly — they only express intent. This enforces
the platform rule that every trade passes through the Risk layer.

Strategies must be deterministic: no wall-clock, no RNG, no I/O.
"""
from abc import ABC, abstractmethod
from datetime import time
from typing import Protocol

from trading_system.replay.events import BarEvent
from trading_system.strategy.signals import Signal


class PortfolioView(Protocol):
    """Read-only portfolio interface exposed to strategies."""

    def has_position(self, symbol: str) -> bool: ...
    def position_qty(self, symbol: str) -> int: ...
    def entry_price(self, symbol: str) -> float: ...
    def bars_held(self, symbol: str) -> int: ...
    def open_symbols(self) -> list[str]: ...


class Strategy(ABC):
    """Base class for all trading strategies."""

    name: str = "base"
    version: str = "v1"
    interval: str = "day"
    # Cross-sectional strategies see ALL symbols at a timestamp at once via on_bars().
    # Per-symbol strategies (the default) are driven one bar at a time via on_bar().
    cross_sectional: bool = False

    # Intraday (same-day) strategies: positions are force-flattened before the close (MIS).
    # When True, the engine (a) suppresses new entries at/after `no_entry_after` and (b) queues
    # a square-off SELL for any open position at/after `square_off_time` — which fills at the
    # NEXT intra-session bar's open (so `square_off_time` must sit ≥1 bar before the session's
    # last bar; 15:15 leaves room for both 5-min and 1-min sessions). Daily/overnight strategies
    # leave this False and are completely unaffected.
    intraday: bool = False
    square_off_time: time = time(15, 15)
    no_entry_after: time = time(15, 15)

    def __init__(self, **params: float) -> None:
        self.params = params

    @abstractmethod
    def on_bar(self, bar: BarEvent, portfolio: PortfolioView) -> list[Signal]:
        """Return signals for this bar. Empty list = do nothing."""
        ...

    def on_bars(self, bars: list[BarEvent], portfolio: PortfolioView) -> list[Signal]:
        """Return signals for all bars sharing one timestamp (cross-sectional entry point).

        Default fans out to on_bar() per symbol so per-symbol strategies need no changes.
        Cross-sectional strategies override this and set cross_sectional = True.
        """
        signals: list[Signal] = []
        for bar in bars:
            signals.extend(self.on_bar(bar, portfolio))
        return signals

    def describe(self) -> dict:  # type: ignore[type-arg]
        """Params snapshot for the audit log / reproducibility hash."""
        return {"name": self.name, "version": self.version, "interval": self.interval, "params": self.params}
