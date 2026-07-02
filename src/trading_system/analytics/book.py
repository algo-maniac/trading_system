"""Capital-partitioned multi-sleeve book.

Combine N independent strategy "sleeves", each allocated a fixed fraction of total
capital. Unlike MultiStrategy (one shared portfolio + signal-merge), this runs each
sleeve as a fully independent backtest on its slice of capital, then SUMS the per-
timestamp equity curves. This is the correct model when:
  - sleeves use different engine paths — a cross-sectional strategy (batched on_bars)
    cannot share a per-symbol signal loop with a per-symbol strategy (streaming on_bar);
  - a sleeve runs near-fully-invested — in a shared pool it would starve the others.

The blended max-drawdown and Sharpe are computed from the summed curve, so they capture
the decorrelation benefit: drawdowns that don't coincide in time partially cancel.

Return math: if sleeve i has weight w_i (Σw_i = 1) and return r_i, the blended book
return is exactly Σ w_i·r_i — but the blended DD and Sharpe are NOT weighted averages;
they come from the summed equity path, which is where diversification shows up.
"""
from dataclasses import dataclass, field
from datetime import date

from trading_system.analytics.metrics import compute_metrics
from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.results import BacktestConfig, BacktestResult
from trading_system.execution.fees import Product
from trading_system.observability import get_logger
from trading_system.portfolio.portfolio import EquityPoint
from trading_system.replay.events import BarEvent
from trading_system.strategy.registry import get_strategy

logger = get_logger(__name__)


@dataclass
class Sleeve:
    strategy_name: str
    weight: float
    params: dict = field(default_factory=dict)  # type: ignore[type-arg]


@dataclass
class BookResult:
    # (display_name, weight, weighted-capital BacktestResult) per sleeve
    sleeves: list[tuple[str, float, BacktestResult]]
    combined: BacktestResult
    benchmark_return: float


def _sum_equity_curves(
    curves: list[list[EquityPoint]], initials: list[float]
) -> list[EquityPoint]:
    """Sum N equity curves on the union of their timestamps (forward-filled).

    Before a sleeve's first snapshot, its equity is seeded at its initial capital, so
    a sleeve that starts trading later doesn't drop out of the early sum. peak/drawdown
    are recomputed on the summed path.
    """
    all_ts = sorted({p.ts for c in curves for p in c})
    maps = [{p.ts: p for p in c} for c in curves]
    # Seed each sleeve at its initial capital (all-cash) for timestamps before its first point.
    last: list[EquityPoint] = [
        EquityPoint(ts=all_ts[0], equity=init, cash=init, positions_value=0.0, drawdown=0.0)
        for init in initials
    ] if all_ts else []

    out: list[EquityPoint] = []
    peak = float("-inf")
    for ts in all_ts:
        eq = cash = pv = 0.0
        for i, m in enumerate(maps):
            if ts in m:
                last[i] = m[ts]
            p = last[i]
            eq += p.equity
            cash += p.cash
            pv += p.positions_value
        peak = max(peak, eq)
        dd = (eq - peak) / peak if peak > 0 else 0.0
        out.append(EquityPoint(ts=ts, equity=eq, cash=cash, positions_value=pv, drawdown=dd))
    return out


def combine_sleeves(
    sleeves: list[Sleeve],
    bars: list[BarEvent],
    symbols: list[str],
    interval: str,
    from_date: date,
    to_date: date,
    total_capital: float,
    slippage_bps: float,
    fees_enabled: bool,
    feature_set_version: str,
) -> BookResult:
    """Run each sleeve on weight×total_capital over the same bars, then sum equity curves.

    Weights must be positive and sum to ~1.0. Each sleeve runs its own BacktestEngine
    (cross-sectional sleeves take the batched path automatically), so heterogeneous
    strategy types combine cleanly. Returns per-sleeve results + the blended book.
    """
    if not sleeves:
        raise ValueError("combine_sleeves requires at least one sleeve")
    if any(s.weight <= 0 for s in sleeves):
        raise ValueError("sleeve weights must be positive")
    total_w = sum(s.weight for s in sleeves)
    if abs(total_w - 1.0) > 1e-6:
        raise ValueError(f"sleeve weights must sum to 1.0, got {total_w}")

    per_sleeve: list[tuple[str, float, BacktestResult]] = []
    initials: list[float] = []
    curves: list[list[EquityPoint]] = []
    all_trades = []

    for sleeve in sleeves:
        strat = get_strategy(sleeve.strategy_name, **sleeve.params)
        sleeve_capital = total_capital * sleeve.weight
        product = Product.MIS if getattr(strat, "intraday", False) else Product.CNC
        cfg = BacktestConfig(
            strategy_name=strat.name, strategy_version=strat.version, params=strat.params,
            symbols=symbols, interval=interval, from_date=from_date, to_date=to_date,
            initial_capital=sleeve_capital, slippage_bps=slippage_bps,
            fees_enabled=fees_enabled, feature_set_version=feature_set_version,
            product=product.value,
        )
        res = BacktestEngine(strat, cfg, product=product).run(iter(bars))
        display = f"{strat.name} ({sleeve.weight:.0%})"
        per_sleeve.append((display, sleeve.weight, res))
        initials.append(sleeve_capital)
        curves.append(res.equity_curve)
        all_trades.extend(res.trades)
        logger.info(
            "sleeve_done", strategy=strat.name, weight=sleeve.weight,
            capital=round(sleeve_capital, 2),
            total_return=round(res.metrics.get("total_return", 0.0), 4),
        )

    combined_curve = _sum_equity_curves(curves, initials)
    combined_cfg = BacktestConfig(
        strategy_name="book:" + "+".join(f"{s.strategy_name}*{s.weight:g}" for s in sleeves),
        strategy_version="book", params={s.strategy_name: s.weight for s in sleeves},
        symbols=symbols, interval=interval, from_date=from_date, to_date=to_date,
        initial_capital=total_capital, slippage_bps=slippage_bps,
        fees_enabled=fees_enabled, feature_set_version=feature_set_version,
    )
    combined_metrics = compute_metrics(combined_curve, all_trades, interval, total_capital)
    combined = BacktestResult(
        config=combined_cfg, equity_curve=combined_curve,
        trades=all_trades, signals=[], metrics=combined_metrics,
    )

    # Equal-weight buy-and-hold benchmark over the same universe/period (lazy import: cycle).
    from trading_system.analytics.walkforward import _benchmark_return
    bench = _benchmark_return(bars)

    logger.info(
        "book_done", sleeves=len(sleeves),
        total_return=round(combined_metrics.get("total_return", 0.0), 4),
        sharpe=round(combined_metrics.get("sharpe", 0.0), 2),
        max_dd=round(combined_metrics.get("max_drawdown", 0.0), 4),
    )
    return BookResult(sleeves=per_sleeve, combined=combined, benchmark_return=bench)
