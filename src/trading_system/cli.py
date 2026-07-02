"""Trading System CLI — Phase 1 operator interface.

Commands:
  trading auth               Authenticate with Kite (run once per trading day)
  trading sync-instruments   Pull NSE instrument master into the DB
  trading ingest             Fetch historical OHLCV from Kite → DB
  trading build-features     Compute indicators from stored OHLCV → DB
  trading replay             Print bars in chronological order
  trading verify             Assert replay determinism (headline Phase 1 test)
"""
from datetime import date

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(name="trading", add_completion=False, help="Agentic Trading System — Phase 1")
console = Console()


def _date(value: str) -> date:
    """Parse YYYY-MM-DD string to date. Typer ≥0.12 doesn't support datetime.date natively."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter(f"Expected YYYY-MM-DD, got: {value}")


# Intervals the backtest research path supports (must match calendar + metrics keys).
_ALLOWED_INTERVALS = ("day", "60minute", "15minute", "5minute", "minute")


def _validate_interval(interval: str) -> str:
    if interval not in _ALLOWED_INTERVALS:
        raise typer.BadParameter(
            f"Unsupported interval '{interval}'. Choose one of: {', '.join(_ALLOWED_INTERVALS)}"
        )
    return interval


def _parse_params(items: list[str] | None) -> dict[str, float | int]:
    """Parse repeated --param key=value into a numeric dict (int if integral, else float).

    Strategy params are all numeric (e.g. size_hint=0.15, max_hold_bars=8), so callers can
    re-tune a strategy per interval without editing code.
    """
    params: dict[str, float | int] = {}
    for item in items or []:
        if "=" not in item:
            raise typer.BadParameter(f"--param must be key=value, got: {item}")
        key, _, raw = item.partition("=")
        key, raw = key.strip(), raw.strip()
        try:
            value: float | int = int(raw)
        except ValueError:
            try:
                value = float(raw)
            except ValueError:
                raise typer.BadParameter(f"--param {key} must be numeric, got: {raw}")
        params[key] = value
    return params


def _parse_grid(items: list[str] | None) -> dict[str, list[float]]:
    """Parse repeated -g name=v1,v2,v3 into {name: [v1, v2, v3]} for the parameter sweep."""
    grid: dict[str, list[float]] = {}
    for item in items or []:
        if "=" not in item:
            raise typer.BadParameter(f"-g must be name=v1,v2,..., got: {item}")
        key, _, raw = item.partition("=")
        values: list[float] = []
        for tok in raw.split(","):
            tok = tok.strip()
            try:
                values.append(float(tok))
            except ValueError:
                raise typer.BadParameter(f"-g {key.strip()} values must be numeric, got: {tok}")
        if values:
            grid[key.strip()] = values
    return grid


def _setup(log_level: str = "INFO") -> None:
    from trading_system.observability import setup_logging
    setup_logging(log_level)


@app.callback()
def _callback(
    log_level: str = typer.Option("INFO", envvar="LOG_LEVEL", help="Log level"),
) -> None:
    _setup(log_level)


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

@app.command()
def auth() -> None:
    """Authenticate with Zerodha Kite Connect (required once per trading day)."""
    from trading_system.data.auth import complete_login, get_login_url

    url = get_login_url()
    console.print("\n[bold cyan]Step 1[/] Open this URL in your browser:\n")
    console.print(f"  {url}\n")
    console.print("[bold cyan]Step 2[/] After logging in, paste the redirect URL (or just the request_token):\n")
    raw = typer.prompt("  Redirect URL / request_token")

    # Accept either the full redirect URL or just the bare token
    if "request_token=" in raw:
        token = raw.split("request_token=")[1].split("&")[0]
    else:
        token = raw.strip()

    complete_login(token)
    console.print("\n[bold green]✓ Authenticated![/] Access token cached for today.\n")


# ---------------------------------------------------------------------------
# sync-instruments
# ---------------------------------------------------------------------------

@app.command("sync-instruments")
def sync_instruments() -> None:
    """Sync the NSE instrument master from Kite into the database."""
    from trading_system.data.auth import get_authenticated_kite
    from trading_system.data.instruments import sync_instruments as _sync
    from trading_system.data.storage import get_connection

    kite = get_authenticated_kite()
    with get_connection() as conn:
        count = _sync(kite, conn)
    console.print(f"[bold green]✓ Synced {count:,} instruments.[/]")


@app.command("screen-volatile")
def screen_volatile(
    as_of: str = typer.Option(None, "--as-of", help="Screen date (default: today)"),
    top_n: int = typer.Option(20, "--top-n"),
    lookback_days: int = typer.Option(90, "--lookback-days"),
    min_turnover_cr: float = typer.Option(50.0, "--min-turnover-cr"),
) -> None:
    """Rolling volatile-universe screen: top-N Nifty-200 names by avg intraday range
    (trailing window, liquidity-filtered). This IS the gap-ORB universe — rerun monthly."""
    from datetime import date as _dateclass

    from trading_system.config import get_settings
    from trading_system.data.screener import volatile_universe
    from trading_system.data.storage import get_connection

    s = get_settings()
    _as_of = _date(as_of) if as_of else _dateclass.today()
    with get_connection() as conn:
        selected = volatile_universe(
            conn, s.nifty_200_symbols, _as_of,
            top_n=top_n, lookback_days=lookback_days, min_turnover_cr=min_turnover_cr,
        )
    console.print(f"[bold]Volatile universe as of {_as_of}[/] (top {top_n}, {lookback_days}d window):")
    console.print("  " + " ".join(selected))


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

@app.command()
def ingest(
    symbols: list[str] | None = typer.Option(
        None, "--symbol", "-s", help="Tradingsymbol(s). Default: all NIFTY 50."
    ),
    intervals: list[str] | None = typer.Option(
        None, "--interval", "-i", help="Candle interval(s). Default: day, minute."
    ),
    from_date: str = typer.Option(..., "--from", help="Start date (YYYY-MM-DD)"),
    to_date: str = typer.Option(..., "--to", help="End date (YYYY-MM-DD)"),
    workers: int = typer.Option(
        0, "--workers", "-w",
        help="Parallel symbol workers (0 = use ingest_workers from config).",
    ),
    skip_features: bool = typer.Option(
        False, "--skip-features", help="Store OHLCV only; skip automatic feature computation."
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-fetch the whole range even if already in the DB (skip gap-check)."
    ),
) -> None:
    """Ingest historical OHLCV from Kite Connect into TimescaleDB.

    Gap-aware: days already stored are not re-fetched, so re-running an overlapping range
    makes no redundant Kite API calls (today is always re-pulled — its session may be live).
    Use --force to re-fetch everything regardless of existing coverage.

    By default, features are computed and stored immediately after each symbol's OHLCV
    lands in the DB (all feature-set versions matching the interval are built automatically).
    Use --skip-features to disable this and run build-features separately.
    """
    from trading_system.config import get_settings
    from trading_system.data.ingestion import ingest_universe

    s = get_settings()
    _symbols = symbols or s.nifty_50_symbols
    _intervals = intervals or s.default_intervals
    _from = _date(from_date)
    _to = _date(to_date)
    _workers = workers if workers > 0 else s.ingest_workers

    console.print(
        f"Ingesting [bold]{len(_symbols)}[/] symbol(s) × [bold]{len(_intervals)}[/] interval(s) "
        f"from [cyan]{_from}[/] to [cyan]{_to}[/] "
        f"([bold]{_workers}[/] workers, features={'skip' if skip_features else 'auto'}"
        f"{', force' if force else ''}) …"
    )
    results = ingest_universe(
        _symbols, _intervals, _from, _to,
        workers=_workers, auto_features=not skip_features, force=force,
    )

    table = Table(title="Ingestion Results")
    table.add_column("Symbol", style="bold")
    table.add_column("Interval")
    table.add_column("Fetched", justify="right")
    table.add_column("Stored", justify="right")
    table.add_column("Features", justify="right")
    table.add_column("Status")

    for r in results:
        status = r.get("status", "")
        if status in ("success", "up_to_date"):
            color = "green"
        elif "skip" in status:
            color = "yellow"
        else:
            color = "red"
        feat_total = sum(r.get("features_stored", {}).values())
        feat_str = f"{feat_total:,}" if feat_total else ("-" if skip_features else "0")
        table.add_row(
            r.get("symbol", ""),
            r.get("interval", ""),
            str(r.get("rows_fetched", "")),
            str(r.get("rows_stored", "")),
            feat_str,
            f"[{color}]{status}[/{color}]",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# build-features
# ---------------------------------------------------------------------------

@app.command("build-features")
def build_features(
    symbols: list[str] | None = typer.Option(None, "--symbol", "-s"),
    intervals: list[str] | None = typer.Option(None, "--interval", "-i"),
    from_date: str = typer.Option(..., "--from"),
    to_date: str = typer.Option(..., "--to"),
    version: str = typer.Option("v1", "--version", help="Feature set version"),
) -> None:
    """Compute technical indicators from stored OHLCV and write to the feature store."""
    from trading_system.config import get_settings
    from trading_system.data.storage import get_connection, get_symbol_token_map
    from trading_system.features.store import build_features_for_symbol

    s = get_settings()
    _symbols = symbols or s.nifty_50_symbols
    _intervals = intervals or s.default_intervals
    _from = _date(from_date)
    _to = _date(to_date)

    with get_connection() as conn:
        token_map = get_symbol_token_map(conn, _symbols)
        for symbol in _symbols:
            token = token_map.get(symbol)
            if not token:
                console.print(f"[yellow]Skip {symbol}: not in DB (run sync-instruments)[/]")
                continue
            for interval in _intervals:
                count = build_features_for_symbol(
                    conn, token, symbol, interval, _from, _to, version
                )
                console.print(f"  {symbol:15} {interval:8} → {count:>8,} feature records")

    console.print("[bold green]✓ Features built.[/]")


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

@app.command()
def replay(
    symbols: list[str] | None = typer.Option(None, "--symbol", "-s"),
    interval: str = typer.Option("day", "--interval", "-i"),
    from_date: str = typer.Option(..., "--from"),
    to_date: str = typer.Option(..., "--to"),
    limit: int = typer.Option(20, "--limit", help="Max bars to print (0 = all)"),
    version: str = typer.Option("v1", "--version"),
) -> None:
    """Replay stored bars in deterministic chronological order."""
    from trading_system.config import get_settings
    from trading_system.data.storage import get_connection, get_symbol_token_map
    from trading_system.replay.engine import ReplayEngine

    s = get_settings()
    _symbols = symbols or s.nifty_50_symbols[:5]
    _from = _date(from_date)
    _to = _date(to_date)

    with get_connection() as conn:
        token_map = get_symbol_token_map(conn, _symbols)

    engine = ReplayEngine(_symbols, token_map, interval, _from, _to, version)
    count = 0
    for bar in engine.run():
        if limit and count >= limit:
            console.print(f"[dim]… limited to {limit} bars. Use --limit 0 for all.[/]")
            break
        console.print(
            f"{bar.ts.isoformat()} | {bar.symbol:<15} | "
            f"O={bar.open:>10.2f} H={bar.high:>10.2f} L={bar.low:>10.2f} "
            f"C={bar.close:>10.2f} V={bar.volume:>12,}"
        )
        count += 1
    console.print(f"[bold]Total bars printed: {count}[/]")


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

@app.command()
def verify(
    symbols: list[str] | None = typer.Option(None, "--symbol", "-s"),
    interval: str = typer.Option("day", "--interval", "-i"),
    from_date: str = typer.Option(..., "--from"),
    to_date: str = typer.Option(..., "--to"),
    version: str = typer.Option("v1", "--version"),
) -> None:
    """Run data integrity checks and verify replay determinism (Phase 1 success criterion)."""
    from trading_system.config import get_settings
    from trading_system.data.storage import get_connection, get_symbol_token_map
    from trading_system.replay.engine import ReplayEngine

    s = get_settings()
    _symbols = symbols or s.nifty_50_symbols[:5]
    _from = _date(from_date)
    _to = _date(to_date)

    with get_connection() as conn:
        token_map = get_symbol_token_map(conn, _symbols)

    console.print("[bold]Verifying replay determinism …[/]")
    engine = ReplayEngine(_symbols, token_map, interval, _from, _to, version)
    h1 = engine.compute_hash()
    h2 = engine.compute_hash()

    if h1 == h2:
        console.print("[bold green]✓ PASS[/] Replay is deterministic.")
        console.print(f"  SHA-256 prefix: [cyan]{h1[:32]}…[/]")
    else:
        console.print("[bold red]✗ FAIL[/] Replay is NOT deterministic!")
        console.print(f"  Run 1: {h1}")
        console.print(f"  Run 2: {h2}")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# backtest (Phase 2)
# ---------------------------------------------------------------------------

@app.command()
def backtest(
    strategy: str = typer.Option(..., "--strategy", help="Strategy name (momentum, mean_reversion)"),
    symbols: list[str] | None = typer.Option(None, "--symbol", "-s"),
    interval: str = typer.Option("day", "--interval", "-i"),
    from_date: str = typer.Option(..., "--from"),
    to_date: str = typer.Option(..., "--to"),
    capital: float = typer.Option(None, "--capital", help="Initial capital (default from settings)"),
    slippage_bps: float = typer.Option(None, "--slippage-bps"),
    no_fees: bool = typer.Option(False, "--no-fees", help="Disable Zerodha charges (for cost-sanity checks)"),
    version: str = typer.Option("v1", "--feature-version", help="Feature set version"),
    params: list[str] | None = typer.Option(
        None, "--param", "-p", help="Override strategy param, e.g. -p size_hint=0.15 (repeatable)"
    ),
    no_save: bool = typer.Option(False, "--no-save", help="Don't persist the run to the DB"),
    max_position_pct: float = typer.Option(
        None, "--max-position-pct", help="Max fraction of equity per position (default 0.10)"
    ),
    max_positions: int = typer.Option(
        None, "--max-positions", help="Max concurrent open positions (default 10)"
    ),
) -> None:
    """Run a backtest: Strategy → Risk → Execution → Portfolio → Analytics."""
    from trading_system.analytics.report import render_report
    from trading_system.backtest.engine import BacktestEngine
    from trading_system.backtest.persistence import save_result
    from trading_system.backtest.results import BacktestConfig
    from trading_system.config import get_settings
    from trading_system.data.storage import get_connection, get_symbol_token_map
    from trading_system.replay.engine import ReplayEngine
    from trading_system.strategy.registry import get_strategy, list_strategies

    s = get_settings()
    _symbols = symbols or s.nifty_50_symbols
    _interval = _validate_interval(interval)
    _from = _date(from_date)
    _to = _date(to_date)
    _capital = capital if capital is not None else s.backtest_initial_capital
    _slippage = slippage_bps if slippage_bps is not None else s.backtest_slippage_bps
    _params = _parse_params(params)

    try:
        strat = get_strategy(strategy, **_params)
    except KeyError:
        console.print(f"[red]Unknown strategy '{strategy}'. Available: {list_strategies()}[/]")
        raise typer.Exit(code=1)
    except TypeError as exc:
        console.print(f"[red]Bad --param for '{strategy}': {exc}[/]")
        raise typer.Exit(code=1)

    with get_connection() as conn:
        token_map = get_symbol_token_map(conn, _symbols)

    # Intraday strategies trade MIS (square-off, lower charges); others trade CNC delivery.
    from trading_system.execution.fees import Product
    _product = Product.MIS if getattr(strat, "intraday", False) else Product.CNC

    cfg = BacktestConfig(
        strategy_name=strat.name, strategy_version=strat.version, params=strat.params,
        symbols=_symbols, interval=_interval, from_date=_from, to_date=_to,
        initial_capital=_capital, slippage_bps=_slippage, fees_enabled=not no_fees,
        feature_set_version=version, product=_product.value,
    )

    # Optional risk-cap overrides (sizing experiments); unspecified fields keep RiskConfig defaults.
    _risk = None
    if max_position_pct is not None or max_positions is not None:
        from trading_system.risk.models import RiskConfig
        _d = RiskConfig()
        _risk = RiskConfig(
            max_position_pct=_d.max_position_pct if max_position_pct is None else max_position_pct,
            max_positions=_d.max_positions if max_positions is None else max_positions,
        )

    console.print(f"[bold]Running backtest[/] {strat.name} {strat.version} …")
    bars = ReplayEngine(_symbols, token_map, _interval, _from, _to, version)
    engine = BacktestEngine(strat, cfg, risk_config=_risk, product=_product)
    result = engine.run(bars.run())

    run_id = None
    if not no_save:
        run_id = save_result(result)

    render_report(result, console, run_id=run_id)


@app.command("backtest-rolling")
def backtest_rolling(
    strategy: str = typer.Option(..., "--strategy", help="Strategy name"),
    symbols: list[str] | None = typer.Option(None, "--symbol", "-s"),
    interval: str = typer.Option("day", "--interval", "-i"),
    from_date: str = typer.Option(..., "--from"),
    to_date: str = typer.Option(..., "--to"),
    window_days: int = typer.Option(120, "--window-days", help="Length of each window (calendar days)"),
    step_days: int = typer.Option(30, "--step-days", help="Stride between windows (calendar days)"),
    capital: float = typer.Option(None, "--capital"),
    slippage_bps: float = typer.Option(None, "--slippage-bps"),
    no_fees: bool = typer.Option(False, "--no-fees"),
    version: str = typer.Option("v1", "--feature-version"),
    params: list[str] | None = typer.Option(None, "--param", "-p", help="Override strategy param (repeatable)"),
    no_save: bool = typer.Option(False, "--no-save", help="Don't persist the rolling run to the DB"),
) -> None:
    """Sweep a strategy across sliding date windows and report robustness across periods."""
    from trading_system.analytics.report import render_rolling_report
    from trading_system.analytics.walkforward import run_rolling
    from trading_system.backtest.persistence import save_rolling_result
    from trading_system.config import get_settings
    from trading_system.data.storage import get_connection, get_symbol_token_map
    from trading_system.strategy.registry import get_strategy, list_strategies

    s = get_settings()
    _symbols = symbols or s.nifty_50_symbols
    _interval = _validate_interval(interval)
    _from = _date(from_date)
    _to = _date(to_date)
    _capital = capital if capital is not None else s.backtest_initial_capital
    _slippage = slippage_bps if slippage_bps is not None else s.backtest_slippage_bps
    _params = _parse_params(params)

    try:
        get_strategy(strategy, **_params)  # validate name + params early
    except KeyError:
        console.print(f"[red]Unknown strategy '{strategy}'. Available: {list_strategies()}[/]")
        raise typer.Exit(code=1)
    except TypeError as exc:
        console.print(f"[red]Bad --param for '{strategy}': {exc}[/]")
        raise typer.Exit(code=1)

    with get_connection() as conn:
        token_map = get_symbol_token_map(conn, _symbols)

    console.print(
        f"[bold]Running rolling backtest[/] {strategy} · "
        f"window={window_days}d step={step_days}d …"
    )
    result = run_rolling(
        strategy_name=strategy, params=_params, symbols=_symbols, token_map=token_map,
        interval=_interval, from_date=_from, to_date=_to,
        window_days=window_days, step_days=step_days,
        initial_capital=_capital, slippage_bps=_slippage, fees_enabled=not no_fees,
        feature_set_version=version,
    )

    rolling_run_id = None
    if not no_save and result.windows:
        rolling_run_id = save_rolling_result(result)

    render_rolling_report(result, console, run_id=rolling_run_id)


@app.command("backtest-sweep")
def backtest_sweep(
    strategy: str = typer.Option(..., "--strategy", help="Strategy name"),
    grid: list[str] | None = typer.Option(
        None, "--grid", "-g", help="Param axis name=v1,v2,v3 (repeatable), e.g. z_entry=1.0,1.5,2.0"
    ),
    symbols: list[str] | None = typer.Option(None, "--symbol", "-s"),
    interval: str = typer.Option("day", "--interval", "-i"),
    from_date: str = typer.Option(..., "--from"),
    to_date: str = typer.Option(..., "--to"),
    rank_by: str = typer.Option("sharpe", "--rank-by", help="Ranking metric, e.g. sharpe"),
    top_k: int = typer.Option(5, "--top-k", help="How many top configs to stability-check"),
    stability_window_days: int = typer.Option(0, "--stability-window-days",
        help="Rolling-window length for the top-K cross-check (0 = auto by interval)"),
    capital: float = typer.Option(None, "--capital"),
    slippage_bps: float = typer.Option(None, "--slippage-bps"),
    no_fees: bool = typer.Option(False, "--no-fees"),
    version: str = typer.Option("v1", "--feature-version"),
) -> None:
    """Grid-search a strategy's parameters and rank configs (default by Sharpe), then
    cross-check the top-K on rolling windows to flag overfit winners."""
    from trading_system.analytics.report import render_sweep
    from trading_system.analytics.sweep import run_sweep
    from trading_system.analytics.walkforward import run_rolling
    from trading_system.backtest.results import BacktestConfig
    from trading_system.config import get_settings
    from trading_system.data.storage import get_connection, get_symbol_token_map
    from trading_system.replay.engine import ReplayEngine
    from trading_system.strategy.registry import get_strategy, list_strategies

    s = get_settings()
    _symbols = symbols or s.nifty_50_symbols
    _interval = _validate_interval(interval)
    _from, _to = _date(from_date), _date(to_date)
    _capital = capital if capital is not None else s.backtest_initial_capital
    _slippage = slippage_bps if slippage_bps is not None else s.backtest_slippage_bps
    _grid = _parse_grid(grid)
    if not _grid:
        console.print("[red]Provide at least one -g name=v1,v2,... axis to sweep.[/]")
        raise typer.Exit(code=1)
    try:
        get_strategy(strategy)  # validate name early
    except KeyError:
        console.print(f"[red]Unknown strategy '{strategy}'. Available: {list_strategies()}[/]")
        raise typer.Exit(code=1) from None

    with get_connection() as conn:
        token_map = get_symbol_token_map(conn, _symbols)

    # Auto window for the stability cross-check: short for intraday, ~quarter for daily/swing.
    win = stability_window_days or (20 if _interval != "day" else 120)
    step = max(1, win // 4)

    def stability_fn(params: dict) -> dict:  # type: ignore[type-arg]
        return run_rolling(
            strategy_name=strategy, params=params, symbols=_symbols, token_map=token_map,
            interval=_interval, from_date=_from, to_date=_to,
            window_days=win, step_days=step, initial_capital=_capital,
            slippage_bps=_slippage, fees_enabled=not no_fees, feature_set_version=version,
        ).summary

    base_cfg = BacktestConfig(
        strategy_name=strategy, strategy_version="", params={}, symbols=_symbols,
        interval=_interval, from_date=_from, to_date=_to, initial_capital=_capital,
        slippage_bps=_slippage, fees_enabled=not no_fees, feature_set_version=version,
    )
    bars = list(ReplayEngine(_symbols, token_map, _interval, _from, _to, version).run())
    console.print(
        f"[bold]Sweeping[/] {strategy}: {sum(len(v) for v in _grid.values())} values across "
        f"{len(_grid)} axes over {len(bars)} bars · ranking by {rank_by} …"
    )
    result = run_sweep(
        strategy, _grid, base_cfg, bars,
        rank_by=rank_by, top_k=top_k, stability_fn=stability_fn,
    )
    render_sweep(result, console)


@app.command("backtest-multi")
def backtest_multi(
    strategies: list[str] = typer.Option(
        ..., "--strategy", help="Strategy name (repeatable, ≥2 required)"
    ),
    symbols: list[str] | None = typer.Option(None, "--symbol", "-s"),
    interval: str = typer.Option("day", "--interval", "-i"),
    from_date: str = typer.Option(..., "--from"),
    to_date: str = typer.Option(..., "--to"),
    capital: float = typer.Option(None, "--capital"),
    slippage_bps: float = typer.Option(None, "--slippage-bps"),
    no_fees: bool = typer.Option(False, "--no-fees"),
    version: str = typer.Option("v2", "--feature-version"),
    params: list[str] | None = typer.Option(
        None, "--param", "-p",
        help="Per-strategy param, prefixed by name: strategy_name.key=val (repeatable)",
    ),
    no_save: bool = typer.Option(False, "--no-save"),
) -> None:
    """Run multiple strategies on a shared capital pool and compare blended vs. standalone.

    Each strategy runs standalone first (baseline), then all run combined on the same
    capital.  Reports Sharpe, return, alpha vs. equal-weight B&H, and diversification
    benefit (Δ Sharpe, Δ max-drawdown).

    Params are prefixed by strategy name:
      -p swing_pullback.rsi_max=30 -p momentum_v3.trailing_atr_mult=3
    """
    from trading_system.analytics.report import render_combined_report
    from trading_system.analytics.walkforward import _benchmark_return
    from trading_system.backtest.engine import BacktestEngine
    from trading_system.backtest.persistence import save_result
    from trading_system.backtest.results import BacktestConfig
    from trading_system.config import get_settings
    from trading_system.data.storage import get_connection, get_symbol_token_map
    from trading_system.execution.fees import Product
    from trading_system.replay.engine import ReplayEngine
    from trading_system.strategy.multi import MultiStrategy
    from trading_system.strategy.registry import get_strategy, list_strategies

    if len(strategies) < 2:
        console.print("[red]backtest-multi requires at least 2 --strategy args.[/]")
        raise typer.Exit(code=1)

    s = get_settings()
    _symbols = symbols or s.nifty_50_symbols
    _interval = _validate_interval(interval)
    _from = _date(from_date)
    _to = _date(to_date)
    _capital = capital if capital is not None else s.backtest_initial_capital
    _slippage = slippage_bps if slippage_bps is not None else s.backtest_slippage_bps

    # Parse per-strategy params: format is strategy_name.param=value
    per_params: dict[str, dict[str, float | int]] = {n: {} for n in strategies}
    for item in params or []:
        if "=" not in item:
            console.print(f"[red]--param must be key=value, got: {item}[/]")
            raise typer.Exit(code=1)
        key, _, raw = item.partition("=")
        key, raw = key.strip(), raw.strip()
        matched, pname = None, key
        for sname in strategies:
            if key.startswith(f"{sname}."):
                matched, pname = sname, key[len(sname) + 1:]
                break
        if matched is None:
            console.print(
                f"[red]--param '{key}' must be prefixed with a strategy name, "
                f"e.g. --param {strategies[0]}.{key}=...[/]"
            )
            raise typer.Exit(code=1)
        try:
            pval: float | int = int(raw)
        except ValueError:
            try:
                pval = float(raw)
            except ValueError:
                console.print(f"[red]--param {key} must be numeric, got: {raw}[/]")
                raise typer.Exit(code=1)
        per_params[matched][pname] = pval

    # Validate strategy names + params up-front
    for sname in strategies:
        try:
            get_strategy(sname, **per_params[sname])
        except KeyError:
            console.print(
                f"[red]Unknown strategy '{sname}'. Available: {list_strategies()}[/]"
            )
            raise typer.Exit(code=1)
        except TypeError as exc:
            console.print(f"[red]Bad --param for '{sname}': {exc}[/]")
            raise typer.Exit(code=1)

    with get_connection() as conn:
        token_map = get_symbol_token_map(conn, _symbols)

    console.print(
        f"[bold]Loading bars[/] {len(_symbols)} symbols × {_interval} "
        f"({_from} → {_to}, feature_version={version}) …"
    )
    bars = list(ReplayEngine(_symbols, token_map, _interval, _from, _to, version).run())
    bench = _benchmark_return(bars)
    console.print(
        f"Loaded [bold]{len(bars):,}[/] bars · "
        f"benchmark equal-weight B&H = {bench * 100:.2f}%\n"
    )

    def _make_strats():
        return [get_strategy(n, **per_params[n]) for n in strategies]

    first_strats = _make_strats()
    _product = (
        Product.MIS
        if any(getattr(st, "intraday", False) for st in first_strats)
        else Product.CNC
    )

    def _run_one(strat):
        cfg = BacktestConfig(
            strategy_name=strat.name, strategy_version=strat.version,
            params=strat.params, symbols=_symbols, interval=_interval,
            from_date=_from, to_date=_to, initial_capital=_capital,
            slippage_bps=_slippage, fees_enabled=not no_fees,
            feature_set_version=version, product=_product.value,
        )
        return BacktestEngine(strat, cfg, product=_product).run(iter(bars))

    # Standalone runs (one fresh instance each)
    standalone = []
    for strat in first_strats:
        console.print(f"[bold]Standalone[/] {strat.name} {strat.version} …")
        res = _run_one(strat)
        m = res.metrics
        console.print(
            f"  → Return {m.get('total_return', 0) * 100:.2f}%  "
            f"Sharpe {m.get('sharpe', 0):.2f}  "
            f"Trades {m.get('num_trades', 0)}"
        )
        standalone.append((strat.name, res))
        if not no_save:
            save_result(res)

    # Combined run (fresh instances — strategies are stateful)
    fresh = _make_strats()
    multi = MultiStrategy(fresh)
    console.print(f"\n[bold]Combined[/] {multi.name} …")
    combined_res = _run_one(multi)
    m = combined_res.metrics
    console.print(
        f"  → Return {m.get('total_return', 0) * 100:.2f}%  "
        f"Sharpe {m.get('sharpe', 0):.2f}  "
        f"Trades {m.get('num_trades', 0)}"
    )
    if not no_save:
        save_result(combined_res)

    render_combined_report(standalone, combined_res, bench, console)


@app.command("backtest-book")
def backtest_book(
    sleeves: list[str] = typer.Option(
        ..., "--sleeve",
        help="Sleeve as name:weight (repeatable), e.g. --sleeve xsec_momentum:0.7 "
             "--sleeve swing_pullback:0.3. Weights must sum to 1.0.",
    ),
    symbols: list[str] | None = typer.Option(None, "--symbol", "-s"),
    interval: str = typer.Option("day", "--interval", "-i"),
    from_date: str = typer.Option(..., "--from"),
    to_date: str = typer.Option(..., "--to"),
    capital: float = typer.Option(None, "--capital"),
    slippage_bps: float = typer.Option(None, "--slippage-bps"),
    no_fees: bool = typer.Option(False, "--no-fees"),
    version: str = typer.Option("v3", "--feature-version"),
    params: list[str] | None = typer.Option(
        None, "--param", "-p",
        help="Per-sleeve param, prefixed by strategy name: name.key=val (repeatable)",
    ),
) -> None:
    """Capital-partitioned book: allocate a fixed % of capital to each strategy sleeve,
    run each independently, and SUM the equity curves.

    Use this (not backtest-multi) when sleeves have different engine types (e.g. a
    cross-sectional momentum + a per-symbol swing) or when a sleeve runs fully invested.
    Blended Sharpe / max-DD come from the summed curve, so they show the diversification
    benefit.  Example:
      trading backtest-book --sleeve xsec_momentum:0.7 --sleeve swing_pullback:0.3 \\
        --feature-version v3 --from 2024-01-01 --to 2025-12-31 -p swing_pullback.rsi_max=30
    """
    from trading_system.analytics.book import Sleeve, combine_sleeves
    from trading_system.analytics.report import render_combined_report
    from trading_system.config import get_settings
    from trading_system.data.storage import get_connection, get_symbol_token_map
    from trading_system.replay.engine import ReplayEngine
    from trading_system.strategy.registry import get_strategy, list_strategies

    # Parse name:weight sleeve specs
    parsed: list[tuple[str, float]] = []
    for spec in sleeves:
        if ":" not in spec:
            console.print(f"[red]--sleeve must be name:weight, got: {spec}[/]")
            raise typer.Exit(code=1)
        name, _, w = spec.partition(":")
        try:
            weight = float(w)
        except ValueError:
            console.print(f"[red]--sleeve weight must be numeric, got: {w}[/]")
            raise typer.Exit(code=1) from None
        parsed.append((name.strip(), weight))

    if len(parsed) < 2:
        console.print("[red]backtest-book requires at least 2 --sleeve args.[/]")
        raise typer.Exit(code=1)
    total_w = sum(w for _, w in parsed)
    if abs(total_w - 1.0) > 1e-6:
        console.print(f"[red]Sleeve weights must sum to 1.0, got {total_w:g}.[/]")
        raise typer.Exit(code=1)

    s = get_settings()
    # Default to the wider Nifty-200 universe: the validated book (Phase 5R) relies on
    # cross-sectional diversification, which the 50-name universe can't provide.
    _symbols = symbols or s.nifty_200_symbols
    _interval = _validate_interval(interval)
    _from, _to = _date(from_date), _date(to_date)
    _capital = capital if capital is not None else s.backtest_initial_capital
    _slippage = slippage_bps if slippage_bps is not None else s.backtest_slippage_bps

    # Per-sleeve params: strategy_name.key=value
    names = [n for n, _ in parsed]
    per_params: dict[str, dict[str, float | int]] = {n: {} for n in names}
    for item in params or []:
        if "=" not in item:
            console.print(f"[red]--param must be key=value, got: {item}[/]")
            raise typer.Exit(code=1)
        key, _, raw = item.partition("=")
        key, raw = key.strip(), raw.strip()
        matched, pname = None, key
        for sname in names:
            if key.startswith(f"{sname}."):
                matched, pname = sname, key[len(sname) + 1:]
                break
        if matched is None:
            console.print(f"[red]--param '{key}' must be prefixed with a sleeve name.[/]")
            raise typer.Exit(code=1)
        try:
            pval: float | int = int(raw)
        except ValueError:
            try:
                pval = float(raw)
            except ValueError:
                console.print(f"[red]--param {key} must be numeric, got: {raw}[/]")
                raise typer.Exit(code=1) from None
        per_params[matched][pname] = pval

    # Validate strategy names + params early
    for sname in names:
        try:
            get_strategy(sname, **per_params[sname])
        except KeyError:
            console.print(f"[red]Unknown strategy '{sname}'. Available: {list_strategies()}[/]")
            raise typer.Exit(code=1) from None
        except TypeError as exc:
            console.print(f"[red]Bad --param for '{sname}': {exc}[/]")
            raise typer.Exit(code=1) from None

    with get_connection() as conn:
        token_map = get_symbol_token_map(conn, _symbols)

    console.print(
        f"[bold]Loading bars[/] {len(_symbols)} symbols × {_interval} "
        f"({_from} → {_to}, feature_version={version}) …"
    )
    bars = list(ReplayEngine(_symbols, token_map, _interval, _from, _to, version).run())

    sleeve_objs = [Sleeve(n, w, per_params[n]) for n, w in parsed]
    console.print(
        f"[bold]Book:[/] " + " + ".join(f"{n} {w:.0%}" for n, w in parsed)
        + f" on ₹{_capital:,.0f}\n"
    )
    result = combine_sleeves(
        sleeve_objs, bars, _symbols, _interval, _from, _to,
        total_capital=_capital, slippage_bps=_slippage,
        fees_enabled=not no_fees, feature_set_version=version,
    )

    standalone = [(name, res) for name, _w, res in result.sleeves]
    render_combined_report(standalone, result.combined, result.benchmark_return, console)


@app.command()
def backtests(limit: int = typer.Option(20, "--limit")) -> None:
    """List past backtest runs."""
    from trading_system.data.storage import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, strategy_name, strategy_version, interval, from_date, to_date,
                   metrics->>'total_return' AS total_return,
                   metrics->>'sharpe' AS sharpe,
                   metrics->>'num_trades' AS num_trades,
                   created_at
            FROM backtest_runs ORDER BY created_at DESC LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()

    if not rows:
        console.print("[dim]No backtest runs yet.[/]")
        return

    table = Table(title="Backtest Runs")
    for col in ("ID", "Strategy", "Ver", "Interval", "From", "To", "Return", "Sharpe", "Trades", "When"):
        table.add_column(col)
    for r in rows:
        tr = f"{float(r[6]) * 100:.2f}%" if r[6] is not None else "-"
        sh = f"{float(r[7]):.2f}" if r[7] is not None else "-"
        table.add_row(
            str(r[0]), r[1], r[2], r[3], str(r[4]), str(r[5]),
            tr, sh, str(r[8] or "-"), r[9].strftime("%Y-%m-%d %H:%M"),
        )
    console.print(table)


@app.command("backtest-report")
def backtest_report(run_id: int = typer.Argument(..., help="Backtest run ID")) -> None:
    """Show full metrics for a stored backtest run."""
    import json

    from trading_system.data.storage import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT strategy_name, strategy_version, interval, from_date, to_date, "
            "initial_capital, fees_enabled, slippage_bps, metrics FROM backtest_runs WHERE id = %s",
            (run_id,),
        )
        row = cur.fetchone()
        if row is None:
            console.print(f"[red]No backtest run with id {run_id}[/]")
            raise typer.Exit(code=1)
        cur.execute("SELECT COUNT(*) FROM backtest_trades WHERE run_id = %s", (run_id,))
        trade_count = cur.fetchone()[0]  # type: ignore[index]

    metrics = row[8] if isinstance(row[8], dict) else json.loads(row[8])
    console.print(f"\n[bold cyan]Backtest run #{run_id}: {row[0]} {row[1]}[/]")
    console.print(f"[dim]{row[3]} → {row[4]} · {row[2]} · capital ₹{float(row[5]):,.0f} · "
                  f"fees={'on' if row[6] else 'OFF'} · slippage={row[7]}bps · {trade_count} trades[/]\n")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    for k, v in metrics.items():
        if isinstance(v, float):
            table.add_row(k, f"{v:,.4f}")
        else:
            table.add_row(k, str(v))
    console.print(table)


if __name__ == "__main__":
    app()
