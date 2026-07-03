"""LivePortfolio: ledger-backed portfolio state for the live (paper) pipeline.

Adapts the persistent ledger (live_* tables) to BOTH consumer interfaces:
  - `strategy.base.PortfolioView` (has_position / position_qty / entry_price /
    bars_held / open_symbols) — used by the Strategist's strategies;
  - the surface `RiskManager.evaluate()` touches (positions / cash / equity() /
    gross_exposure() / position_qty / has_position) — used by the Trader's gate.

Prices: valuation uses the freshest close seen on the bar stream (`update_price`),
falling back to a position's avg_price before its first tick — the same convention
as the backtest Portfolio. `bars_held` approximates trading days with weekday count
(numpy busday_count); NSE holidays make it overcount by ≤1-2 days on long holds,
which is immaterial for max_hold≈10 exits and avoids a calendar lookup here.

State is re-read from the ledger on demand (cheap at our position counts) so a
restarted agent sees exactly what a never-restarted one would — the crash-only design.
"""
from datetime import UTC, datetime

import numpy as np

from trading_system.execution.ledger import LedgerRepo, PositionRow


class LivePortfolio:
    def __init__(self, ledger: LedgerRepo, sleeve: str) -> None:
        self._ledger = ledger
        self._sleeve = sleeve
        self._last_price: dict[str, float] = {}
        self._now: datetime | None = None

    # --- live inputs ---------------------------------------------------------

    def update_price(self, symbol: str, price: float, ts: datetime) -> None:
        self._last_price[symbol] = price
        # Belt-and-suspenders: bars from different sources (WebSocket ticks vs. Postgres
        # day-bars) have crashed this comparison once already when one arrived naive and
        # the other tz-aware. The real fix is normalizing at each source; this guard
        # stops any future source regression from taking the whole agent down.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if self._now is None or ts > self._now:
            self._now = ts

    # --- shared internals ----------------------------------------------------

    def _open(self) -> dict[str, PositionRow]:
        return {p.symbol: p for p in self._ledger.open_positions(self._sleeve)}

    # --- PortfolioView (strategies) -------------------------------------------

    def has_position(self, symbol: str) -> bool:
        return self._ledger.open_position(self._sleeve, symbol) is not None

    def position_qty(self, symbol: str) -> int:
        pos = self._ledger.open_position(self._sleeve, symbol)
        return pos.qty if pos else 0

    def entry_price(self, symbol: str) -> float:
        pos = self._ledger.open_position(self._sleeve, symbol)
        return pos.avg_price if pos else 0.0

    def bars_held(self, symbol: str) -> int:
        pos = self._ledger.open_position(self._sleeve, symbol)
        if pos is None or self._now is None:
            return 0
        return int(np.busday_count(pos.opened_ts.date(), self._now.date()))

    def open_symbols(self) -> list[str]:
        return sorted(self._open().keys())

    # --- RiskManager surface ---------------------------------------------------

    @property
    def positions(self) -> dict[str, PositionRow]:
        return self._open()

    @property
    def cash(self) -> float:
        return self._ledger.sleeve_cash(self._sleeve)

    def positions_value(self) -> float:
        return sum(
            p.qty * self._last_price.get(sym, p.avg_price)
            for sym, p in self._open().items()
        )

    def equity(self) -> float:
        return self.cash + self.positions_value()

    def gross_exposure(self) -> float:
        return self.positions_value()
