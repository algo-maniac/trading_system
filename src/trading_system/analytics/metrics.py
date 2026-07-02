"""Performance metrics computed from the equity curve and trade log.

Required by the brief: Sharpe, Profit Factor, Win Rate, Max Drawdown.
Plus: Sortino, total return, CAGR, expectancy, avg win/loss, exposure.

All pure functions — no DB, no I/O.
"""
import math
from collections.abc import Sequence

from trading_system.portfolio.models import Trade
from trading_system.portfolio.portfolio import EquityPoint

# Annualization factor (periods per year) by bar interval.
_PERIODS_PER_YEAR = {
    "day": 252,
    "minute": 252 * 375,
    "60minute": 252 * 6,
    "15minute": 252 * 25,
    "5minute": 252 * 75,
}


def _returns(equity: Sequence[float]) -> list[float]:
    out = []
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        out.append((equity[i] - prev) / prev if prev else 0.0)
    return out


def max_drawdown(equity: Sequence[float]) -> float:
    """Most negative peak-to-trough drawdown (returned as a negative fraction)."""
    peak = -math.inf
    mdd = 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            mdd = min(mdd, (e - peak) / peak)
    return mdd


def sharpe_ratio(returns: Sequence[float], periods_per_year: int) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (mean / std) * math.sqrt(periods_per_year)


def sortino_ratio(returns: Sequence[float], periods_per_year: int) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    downside = [r for r in returns if r < 0]
    if not downside:
        return 0.0
    dvar = sum(r ** 2 for r in downside) / len(returns)
    dstd = math.sqrt(dvar)
    if dstd == 0:
        return 0.0
    return (mean / dstd) * math.sqrt(periods_per_year)


def compute_metrics(
    equity_curve: list[EquityPoint],
    trades: list[Trade],
    interval: str,
    initial_capital: float,
) -> dict:  # type: ignore[type-arg]
    if not equity_curve:
        return {"error": "empty_equity_curve"}

    equity = [p.equity for p in equity_curve]
    rets = _returns(equity)
    ppy = _PERIODS_PER_YEAR.get(interval, 252)

    final_equity = equity[-1]
    total_return = (final_equity - initial_capital) / initial_capital

    n_periods = len(equity)
    years = n_periods / ppy if ppy else 0.0
    cagr = (final_equity / initial_capital) ** (1 / years) - 1 if years > 0 and final_equity > 0 else 0.0

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)  # positive number

    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (math.inf if gross_profit > 0 else 0.0)
    win_rate = len(wins) / len(trades) if trades else 0.0
    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = -gross_loss / len(losses) if losses else 0.0
    expectancy = (sum(t.pnl for t in trades) / len(trades)) if trades else 0.0

    avg_exposure = (
        sum(p.positions_value / p.equity for p in equity_curve if p.equity > 0) / len(equity_curve)
        if equity_curve else 0.0
    )

    return {
        "total_return": total_return,
        "cagr": cagr,
        "sharpe": sharpe_ratio(rets, ppy),
        "sortino": sortino_ratio(rets, ppy),
        "max_drawdown": max_drawdown(equity),
        "profit_factor": profit_factor,
        "win_rate": win_rate,
        "num_trades": len(trades),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "avg_exposure": avg_exposure,
        "final_equity": final_equity,
        "total_fees": sum(t.fees for t in trades),
        "total_slippage": sum(t.slippage for t in trades),
    }
