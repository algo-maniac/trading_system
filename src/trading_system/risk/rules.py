"""Individual risk-rule calculations (pure functions, easily unit-tested)."""
import math

from trading_system.risk.models import RiskConfig


def max_qty_for_position_pct(equity: float, price: float, max_position_pct: float) -> int:
    """Largest qty whose market value stays within max_position_pct of equity."""
    if price <= 0:
        return 0
    return int(math.floor((equity * max_position_pct) / price))


def max_qty_for_exposure(
    equity: float, gross_exposure: float, price: float, max_gross_exposure_pct: float
) -> int:
    """Largest qty that keeps total long exposure within the gross cap."""
    if price <= 0:
        return 0
    budget = equity * max_gross_exposure_pct - gross_exposure
    if budget <= 0:
        return 0
    return int(math.floor(budget / price))


def max_qty_for_cash(cash: float, price: float) -> int:
    """Can't spend more cash than available (fees ignored here — small buffer)."""
    if price <= 0:
        return 0
    return int(math.floor(cash / price))


def can_open_new_position(num_positions: int, max_positions: int, is_new_symbol: bool) -> bool:
    if not is_new_symbol:
        return True  # adding to an existing position doesn't increase the count
    return num_positions < max_positions


def entries_halted(day_return_pct: float, current_drawdown: float, config: RiskConfig) -> str | None:
    """Return a halt reason if new entries should be blocked, else None.

    - day_return_pct: today's portfolio return (negative = loss).
    - current_drawdown: equity drawdown from peak (negative or zero).
    """
    if day_return_pct <= -config.daily_loss_limit_pct:
        return f"daily_loss_limit hit ({day_return_pct:.2%} <= -{config.daily_loss_limit_pct:.2%})"
    if current_drawdown <= -config.max_drawdown_pct:
        return f"max_drawdown hit ({current_drawdown:.2%} <= -{config.max_drawdown_pct:.2%})"
    return None
