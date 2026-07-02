"""Parameter sweep: run a grid of strategy configs over one bar stream and rank them.

The user's "change parameters and backtest to see which works best" — made systematic and
overfitting-aware. Every combination in the grid is backtested on the SAME in-memory bars (loaded
once, like walkforward), then ranked by a chosen metric (default Sharpe). Because grid-search
optimization invites curve-fitting, the top-K configs are additionally cross-checked on rolling
windows via an injected `stability_fn` (the CLI wires this to `walkforward.run_rolling`): a config
that tops the full period but is unstable across windows is the tell-tale of overfitting.

`run_sweep` itself is DB-free and deterministic — it consumes a materialized bar list, so it is
unit-testable on synthetic streams. The rolling cross-check is optional (CLI-only).
"""
import itertools
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace

from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.results import BacktestConfig
from trading_system.execution.fees import Product
from trading_system.observability import get_logger
from trading_system.replay.events import BarEvent
from trading_system.strategy.registry import get_strategy

logger = get_logger(__name__)


@dataclass
class SweepRow:
    params: dict          # type: ignore[type-arg]
    metrics: dict         # type: ignore[type-arg]
    rank: int = 0


@dataclass
class StabilityRow:
    params: dict          # type: ignore[type-arg]
    rolling_summary: dict  # type: ignore[type-arg] — from walkforward._summarize


@dataclass
class SweepResult:
    strategy_name: str
    rank_by: str
    rows: list[SweepRow]                # ranked best-first by `rank_by`
    stability: list[StabilityRow] = field(default_factory=list)  # top-K rolling cross-check


def _grid_combos(param_grid: dict[str, list[float]]) -> list[dict[str, float]]:
    """Cartesian product of the grid. Param names sorted for deterministic combo ordering."""
    if not param_grid:
        return [{}]
    names = sorted(param_grid)
    return [dict(zip(names, values, strict=True)) for values in itertools.product(*(param_grid[n] for n in names))]


def run_sweep(
    strategy_name: str,
    param_grid: dict[str, list[float]],
    base_config: BacktestConfig,
    bars: Iterable[BarEvent],
    *,
    rank_by: str = "sharpe",
    top_k: int = 5,
    stability_fn: Callable[[dict], dict] | None = None,  # type: ignore[type-arg]
) -> SweepResult:
    """Backtest every grid combo over `bars`, rank by `rank_by` (desc), optionally cross-check
    the top-K for stability. Higher is better for every supported metric — including
    max_drawdown, stored as a negative fraction (−0.10 ranks above −0.25)."""
    bars_list = list(bars)  # materialize once; reused across every combo
    combos = _grid_combos(param_grid)
    logger.info("sweep_start", strategy=strategy_name, combos=len(combos), rank_by=rank_by)

    rows: list[SweepRow] = []
    for params in combos:
        strat = get_strategy(strategy_name, **params)
        product = Product.MIS if getattr(strat, "intraday", False) else Product.CNC
        cfg = replace(
            base_config,
            strategy_name=strat.name, strategy_version=strat.version,
            params=params, product=product.value,
        )
        result = BacktestEngine(strat, cfg, product=product).run(iter(bars_list))
        rows.append(SweepRow(params=params, metrics=result.metrics))

    # Stable sort keeps combo order on ties → deterministic ranking.
    rows.sort(key=lambda r: r.metrics.get(rank_by, float("-inf")), reverse=True)
    for i, r in enumerate(rows):
        r.rank = i + 1

    stability: list[StabilityRow] = []
    if stability_fn is not None:
        for r in rows[:top_k]:
            stability.append(StabilityRow(params=r.params, rolling_summary=stability_fn(r.params)))

    logger.info("sweep_done", strategy=strategy_name, best_params=rows[0].params if rows else {})
    return SweepResult(strategy_name=strategy_name, rank_by=rank_by, rows=rows, stability=stability)
