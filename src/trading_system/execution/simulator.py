"""Execution simulator: turn an approved order into a Fill.

Fill price = the bar's open (next-bar-open model — set by the engine) adjusted by
slippage applied *adverse* to the trade direction:
  - BUY  fills higher than the clean price (you pay up)
  - SELL fills lower  than the clean price (you receive less)

Slippage is a deterministic bps figure (no RNG) so backtests stay reproducible.
Fees are the full Zerodha charge model.
"""
from datetime import datetime

from trading_system.execution.fees import Product, compute_charges
from trading_system.portfolio.models import Fill


def simulate_fill(
    ts: datetime,
    symbol: str,
    is_buy: bool,
    qty: int,
    clean_price: float,
    slippage_bps: float,
    product: Product = Product.CNC,
    fees_enabled: bool = True,
) -> Fill:
    """Build a Fill from an approved order at the given clean (pre-slippage) price."""
    slip_factor = slippage_bps / 10_000.0
    if is_buy:
        fill_price = clean_price * (1.0 + slip_factor)
    else:
        fill_price = clean_price * (1.0 - slip_factor)

    slippage_cost = abs(fill_price - clean_price) * qty

    fees = 0.0
    if fees_enabled:
        fees = compute_charges(qty, fill_price, is_buy, product).total

    return Fill(
        ts=ts,
        symbol=symbol,
        side="BUY" if is_buy else "SELL",
        qty=qty,
        price=fill_price,
        fees=fees,
        slippage=slippage_cost,
    )
