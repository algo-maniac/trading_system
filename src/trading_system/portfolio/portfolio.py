"""Portfolio: cash, open positions, realized/unrealized PnL, and the equity curve.

Long-only. Implements the read-only PortfolioView the strategies see, plus the
mutating apply_fill used by the execution layer.

Slippage is already embedded in fill prices (so it flows through avg_price and
realized PnL automatically); the separate slippage figure is kept only for reporting.
"""
from dataclasses import dataclass
from datetime import datetime

from trading_system.portfolio.models import Fill, Position, Trade


@dataclass
class EquityPoint:
    ts: datetime
    equity: float
    cash: float
    positions_value: float
    drawdown: float


class Portfolio:
    def __init__(self, initial_capital: float) -> None:
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.realized_pnl = 0.0
        self.equity_curve: list[EquityPoint] = []
        self._last_price: dict[str, float] = {}
        self._peak_equity = initial_capital

    # --- PortfolioView (read-only, used by strategies) -----------------------

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions

    def position_qty(self, symbol: str) -> int:
        pos = self.positions.get(symbol)
        return pos.qty if pos else 0

    def entry_price(self, symbol: str) -> float:
        pos = self.positions.get(symbol)
        return pos.avg_price if pos else 0.0

    def bars_held(self, symbol: str) -> int:
        pos = self.positions.get(symbol)
        return pos.bars_held if pos else 0

    def open_symbols(self) -> list[str]:
        """Symbols with a currently-open position (for cross-sectional strategies)."""
        return list(self.positions.keys())

    # --- Mutations -----------------------------------------------------------

    def age_position(self, symbol: str) -> None:
        """Increment the holding period for an existing position (call once per new bar)."""
        pos = self.positions.get(symbol)
        if pos:
            pos.bars_held += 1

    def update_price(self, symbol: str, price: float) -> None:
        self._last_price[symbol] = price

    def apply_fill(self, fill: Fill, reason: str = "") -> Trade | None:
        """Apply an executed fill. Returns a Trade if a position was (partly) closed."""
        if fill.side == "BUY":
            self._apply_buy(fill)
            return None
        return self._apply_sell(fill, reason)

    def _apply_buy(self, fill: Fill) -> None:
        self.cash -= fill.qty * fill.price + fill.fees
        pos = self.positions.get(fill.symbol)
        if pos is None:
            self.positions[fill.symbol] = Position(
                symbol=fill.symbol,
                qty=fill.qty,
                avg_price=fill.price,
                entry_ts=fill.ts,
                entry_fees=fill.fees,
                entry_slippage=fill.slippage,
            )
        else:
            total_qty = pos.qty + fill.qty
            pos.avg_price = (pos.qty * pos.avg_price + fill.qty * fill.price) / total_qty
            pos.qty = total_qty
            pos.entry_fees += fill.fees
            pos.entry_slippage += fill.slippage

    def _apply_sell(self, fill: Fill, reason: str) -> Trade | None:
        pos = self.positions.get(fill.symbol)
        if pos is None:
            return None  # nothing to sell (defensive)

        qty = min(fill.qty, pos.qty)
        self.cash += qty * fill.price - fill.fees

        proportion = qty / pos.qty
        entry_fee_alloc = pos.entry_fees * proportion
        entry_slip_alloc = pos.entry_slippage * proportion

        gross_pnl = qty * (fill.price - pos.avg_price)        # slippage already in prices
        net_pnl = gross_pnl - fill.fees - entry_fee_alloc      # subtract cash costs (fees)
        cost_basis = qty * pos.avg_price
        return_pct = net_pnl / cost_basis if cost_basis else 0.0

        self.realized_pnl += net_pnl

        trade = Trade(
            symbol=fill.symbol,
            side="LONG",
            entry_ts=pos.entry_ts,
            entry_price=pos.avg_price,
            exit_ts=fill.ts,
            exit_price=fill.price,
            qty=qty,
            fees=fill.fees + entry_fee_alloc,
            slippage=fill.slippage + entry_slip_alloc,
            pnl=net_pnl,
            return_pct=return_pct,
            exit_reason=reason,
        )
        self.trades.append(trade)

        # Reduce or remove the position
        remaining = pos.qty - qty
        if remaining <= 0:
            del self.positions[fill.symbol]
        else:
            pos.qty = remaining
            pos.entry_fees -= entry_fee_alloc
            pos.entry_slippage -= entry_slip_alloc

        return trade

    # --- Valuation -----------------------------------------------------------

    def positions_value(self) -> float:
        return sum(
            pos.qty * self._last_price.get(sym, pos.avg_price)
            for sym, pos in self.positions.items()
        )

    def equity(self) -> float:
        return self.cash + self.positions_value()

    def gross_exposure(self) -> float:
        """Total long market value (used by risk limits)."""
        return self.positions_value()

    def snapshot(self, ts: datetime) -> EquityPoint:
        eq = self.equity()
        self._peak_equity = max(self._peak_equity, eq)
        drawdown = (eq - self._peak_equity) / self._peak_equity if self._peak_equity else 0.0
        point = EquityPoint(
            ts=ts,
            equity=eq,
            cash=self.cash,
            positions_value=self.positions_value(),
            drawdown=drawdown,
        )
        self.equity_curve.append(point)
        return point
