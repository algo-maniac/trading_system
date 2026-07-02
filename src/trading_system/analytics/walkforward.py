"""Rolling-window robustness analysis.

Runs a strategy across many overlapping date windows and aggregates the per-window
metrics, answering "is this strategy robust across periods?" quantitatively instead
of eyeballing one full-range number.

Efficiency: the full bar stream is loaded ONCE via ReplayEngine, then each window is
served by filtering that in-memory list and feeding a fresh BacktestEngine (the engine
core is a pure Iterable[BarEvent] consumer — no DB re-query per window).

Determinism note: a fresh strategy instance is built per window, so stateful detectors
(e.g. momentum's prior-bar SMA relationship) reset at each window start. Acceptable for
robustness sampling. Features come from the store (computed over full history at build
time), so windows starting mid-series have no warmup gap.
"""
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

from trading_system.analytics.metrics import compute_metrics
from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.results import BacktestConfig
from trading_system.execution.fees import Product
from trading_system.observability import get_logger
from trading_system.replay.engine import ReplayEngine
from trading_system.replay.events import BarEvent
from trading_system.strategy.registry import get_strategy

logger = get_logger(__name__)


def generate_windows(
    from_date: date, to_date: date, window_days: int, step_days: int
) -> list[tuple[date, date]]:
    """Sliding windows of `window_days` calendar days, advancing by `step_days`.

    Only full-length windows whose end is within [from_date, to_date] are emitted, so
    every window covers the same span (comparable metrics). Returns [] if the range is
    shorter than one window.
    """
    if window_days <= 0 or step_days <= 0:
        raise ValueError("window_days and step_days must be positive")

    windows: list[tuple[date, date]] = []
    start = from_date
    while True:
        end = start + timedelta(days=window_days - 1)
        if end > to_date:
            break
        windows.append((start, end))
        start = start + timedelta(days=step_days)
    return windows


@dataclass
class WindowResult:
    window_start: date
    window_end: date
    total_return: float
    sharpe: float
    max_drawdown: float
    num_trades: int
    win_rate: float
    benchmark_return: float = 0.0  # equal-weight B&H of the traded universe
    alpha: float = 0.0             # total_return − benchmark_return
    avg_exposure: float = 0.0      # mean fraction of capital deployed (0=all cash, 1=fully invested)


@dataclass
class RollingResult:
    strategy_name: str
    strategy_version: str
    params: dict  # type: ignore[type-arg]
    symbols: list[str]
    interval: str
    from_date: date
    to_date: date
    window_days: int
    step_days: int
    windows: list[WindowResult] = field(default_factory=list)
    summary: dict = field(default_factory=dict)  # type: ignore[type-arg]


def _benchmark_return(w_bars: list[BarEvent]) -> float:
    """Equal-weight buy-and-hold return for all symbols present in the window.

    For each symbol: (last_close − first_close) / first_close.
    Returns the arithmetic mean across symbols; 0.0 if no symbol has ≥2 bars.
    """
    by_symbol: dict[str, list[BarEvent]] = {}
    for b in w_bars:
        by_symbol.setdefault(b.symbol, []).append(b)

    symbol_returns: list[float] = []
    for bars in by_symbol.values():
        if len(bars) < 2:
            continue
        first_close = min(bars, key=lambda b: b.ts).close
        last_close = max(bars, key=lambda b: b.ts).close
        if first_close > 0:
            symbol_returns.append((last_close - first_close) / first_close)

    return statistics.fmean(symbol_returns) if symbol_returns else 0.0


def _summarize(windows: list[WindowResult]) -> dict:  # type: ignore[type-arg]
    if not windows:
        return {"n_windows": 0}

    returns = [w.total_return for w in windows]
    sharpes = [w.sharpe for w in windows]
    alphas = [w.alpha for w in windows]
    benchmarks = [w.benchmark_return for w in windows]
    exposures = [w.avg_exposure for w in windows]
    profitable = [w for w in windows if w.total_return > 0]
    positive_alpha = [w for w in windows if w.alpha > 0]
    worst = min(windows, key=lambda w: w.total_return)
    best = max(windows, key=lambda w: w.total_return)

    return {
        "n_windows": len(windows),
        "pct_profitable": len(profitable) / len(windows),
        "mean_return": statistics.fmean(returns),
        "median_return": statistics.median(returns),
        "std_return": statistics.pstdev(returns) if len(returns) > 1 else 0.0,
        "min_return": min(returns),
        "max_return": max(returns),
        "mean_sharpe": statistics.fmean(sharpes),
        "median_sharpe": statistics.median(sharpes),
        "min_sharpe": min(sharpes),
        "max_sharpe": max(sharpes),
        "avg_num_trades": statistics.fmean([w.num_trades for w in windows]),
        "worst_window": [worst.window_start.isoformat(), worst.window_end.isoformat(), worst.total_return],
        "best_window": [best.window_start.isoformat(), best.window_end.isoformat(), best.total_return],
        "mean_benchmark": statistics.fmean(benchmarks),
        "mean_alpha": statistics.fmean(alphas),
        "median_alpha": statistics.median(alphas),
        "pct_positive_alpha": len(positive_alpha) / len(windows),
        "mean_exposure": statistics.fmean(exposures),
    }


def _check_feature_warmup(
    all_bars: list[BarEvent],
    windows: list[tuple[date, date]],
    strategy_name: str,
) -> None:
    """Detect leading-warmup NULLs that silently blank early windows.

    Long-lookback features (e.g. sma_200, warmup=200 bars) are NULL until enough
    history exists. Bars during warmup carry an empty feature dict for that key →
    strategy returns no signals → all-cash window → return = 0%. Averaging 0% windows
    into the summary makes the strategy look artificially bad. This function warns when
    that pattern is detected so the user knows to ingest earlier history.
    """
    if not all_bars or not windows:
        return

    # Build per-feature set of trading *dates* where ≥1 bar has the feature present.
    feature_present_dates: dict[str, set[date]] = {}
    all_dates: set[date] = set()
    for b in all_bars:
        d = b.ts.date()
        all_dates.add(d)
        for key in b.features:
            if key not in feature_present_dates:
                feature_present_dates[key] = set()
            feature_present_dates[key].add(d)

    if not feature_present_dates:
        return

    total_dates = len(all_dates)
    first_date = min(all_dates)
    gaps_found: list[str] = []

    for key in sorted(feature_present_dates):
        first_present = min(feature_present_dates[key])
        if first_present == first_date:
            continue  # available from day 1 — no warmup gap

        null_date_count = total_dates - len(feature_present_dates[key])
        null_frac = null_date_count / total_dates
        if null_frac < 0.05:
            continue  # <5% null — negligible data gaps, not structural warmup

        affected = sum(1 for ws, _ in windows if ws < first_present)
        if affected == 0:
            continue

        logger.warning(
            "feature_warmup_gap",
            strategy=strategy_name,
            feature=key,
            null_pct=round(null_frac * 100, 1),
            first_available=first_present.isoformat(),
            windows_blanked=affected,
            hint="ingest earlier data and rebuild features to eliminate blank windows",
        )
        gaps_found.append(key)

    if gaps_found:
        logger.warning(
            "rolling_warmup_bias",
            strategy=strategy_name,
            affected_features=gaps_found,
            hint=(
                "Early windows sit 100% in cash during feature warmup — "
                "their 0% return is not real strategy performance and biases the summary. "
                "Fix: ingest 1+ extra years before your from_date and rebuild features."
            ),
        )


def run_rolling(
    strategy_name: str,
    params: dict,  # type: ignore[type-arg]
    symbols: list[str],
    token_map: dict[str, int],
    interval: str,
    from_date: date,
    to_date: date,
    window_days: int,
    step_days: int,
    initial_capital: float,
    slippage_bps: float,
    fees_enabled: bool,
    feature_set_version: str,
) -> RollingResult:
    """Run `strategy_name` across sliding windows and aggregate per-window metrics."""
    windows = generate_windows(from_date, to_date, window_days, step_days)

    # Load the full bar stream once; windows are served by in-memory filtering.
    all_bars: list[BarEvent] = list(
        ReplayEngine(symbols, token_map, interval, from_date, to_date, feature_set_version).run()
    )
    logger.info(
        "rolling_start", strategy=strategy_name, n_windows=len(windows),
        total_bars=len(all_bars), interval=interval,
    )

    # Warn early if long-lookback features are NULL over the leading portion of the range.
    _check_feature_warmup(all_bars, windows, strategy_name)

    results: list[WindowResult] = []
    for w_start, w_end in windows:
        w_bars = [b for b in all_bars if w_start <= b.ts.date() <= w_end]
        if not w_bars:
            results.append(WindowResult(w_start, w_end, 0.0, 0.0, 0.0, 0, 0.0))
            continue

        # Fresh strategy + engine per window (state resets at window start).
        strat = get_strategy(strategy_name, **params)
        product = Product.MIS if getattr(strat, "intraday", False) else Product.CNC
        cfg = BacktestConfig(
            strategy_name=strat.name, strategy_version=strat.version, params=strat.params,
            symbols=symbols, interval=interval, from_date=w_start, to_date=w_end,
            initial_capital=initial_capital, slippage_bps=slippage_bps,
            fees_enabled=fees_enabled, feature_set_version=feature_set_version,
            product=product.value,
        )
        engine = BacktestEngine(strat, cfg, product=product)
        result = engine.run(iter(w_bars))
        m = compute_metrics(result.equity_curve, result.trades, interval, initial_capital)
        bench = _benchmark_return(w_bars)
        total_ret = m.get("total_return", 0.0)
        results.append(WindowResult(
            window_start=w_start,
            window_end=w_end,
            total_return=total_ret,
            sharpe=m.get("sharpe", 0.0),
            max_drawdown=m.get("max_drawdown", 0.0),
            num_trades=m.get("num_trades", 0),
            win_rate=m.get("win_rate", 0.0),
            benchmark_return=bench,
            alpha=total_ret - bench,
            avg_exposure=m.get("avg_exposure", 0.0),
        ))

    # Resolve version from a built instance (handles default-param strategies).
    version = get_strategy(strategy_name, **params).version
    rolling = RollingResult(
        strategy_name=strategy_name, strategy_version=version, params=params,
        symbols=symbols, interval=interval, from_date=from_date, to_date=to_date,
        window_days=window_days, step_days=step_days, windows=results,
    )
    rolling.summary = _summarize(results)
    logger.info(
        "rolling_done", strategy=strategy_name, n_windows=len(results),
        pct_profitable=round(rolling.summary.get("pct_profitable", 0.0), 4),
    )
    return rolling
