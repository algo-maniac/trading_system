"""Render a backtest result as a console report."""
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from trading_system.analytics.sweep import SweepResult
from trading_system.analytics.walkforward import RollingResult
from trading_system.backtest.results import BacktestResult

# Type alias used by render_combined_report
_StandaloneList = list[tuple[str, BacktestResult]]


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def render_report(result: BacktestResult, console: Console | None = None, run_id: int | None = None) -> None:
    console = console or Console()
    m = result.metrics
    cfg = result.config

    title = f"Backtest: {cfg.strategy_name} {cfg.strategy_version}"
    if run_id is not None:
        title += f"  (run #{run_id})"
    console.print(f"\n[bold cyan]{title}[/]")
    console.print(
        f"[dim]{cfg.from_date} → {cfg.to_date} · {cfg.interval} · {len(cfg.symbols)} symbols · "
        f"capital ₹{cfg.initial_capital:,.0f} · fees={'on' if cfg.fees_enabled else 'OFF'} · "
        f"slippage={cfg.slippage_bps}bps[/]\n"
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("Metric")
    table.add_column("Value", justify="right")

    pf = m.get("profit_factor", 0.0)
    pf_str = "∞" if pf == float("inf") else f"{pf:.2f}"

    table.add_row("Total Return", _pct(m.get("total_return", 0)))
    table.add_row("CAGR", _pct(m.get("cagr", 0)))
    table.add_row("Sharpe", f"{m.get('sharpe', 0):.2f}")
    table.add_row("Sortino", f"{m.get('sortino', 0):.2f}")
    table.add_row("Max Drawdown", _pct(m.get("max_drawdown", 0)))
    table.add_row("Profit Factor", pf_str)
    table.add_row("Win Rate", _pct(m.get("win_rate", 0)))
    table.add_row("Trades", str(m.get("num_trades", 0)))
    table.add_row("Avg Win", f"₹{m.get('avg_win', 0):,.0f}")
    table.add_row("Avg Loss", f"₹{m.get('avg_loss', 0):,.0f}")
    table.add_row("Expectancy/Trade", f"₹{m.get('expectancy', 0):,.0f}")
    table.add_row("Avg Exposure", _pct(m.get("avg_exposure", 0)))
    table.add_row("Final Equity", f"₹{m.get('final_equity', 0):,.0f}")
    table.add_row("Total Fees", f"₹{m.get('total_fees', 0):,.0f}")
    table.add_row("Total Slippage", f"₹{m.get('total_slippage', 0):,.0f}")

    console.print(table)


def render_rolling_report(
    result: RollingResult, console: Console | None = None, run_id: int | None = None,
    max_rows: int = 40,
) -> None:
    """Render a rolling-window robustness report: per-window table + summary panel."""
    console = console or Console()
    s = result.summary

    title = f"Rolling backtest: {result.strategy_name} {result.strategy_version}"
    if run_id is not None:
        title += f"  (run #{run_id})"
    console.print(f"\n[bold cyan]{title}[/]")
    console.print(
        f"[dim]{result.from_date} → {result.to_date} · {result.interval} · "
        f"{len(result.symbols)} symbols · window={result.window_days}d step={result.step_days}d[/]\n"
    )

    if not result.windows:
        console.print("[yellow]No full-length windows fit in the given date range.[/]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Window")
    table.add_column("Return", justify="right")
    table.add_column("Bench", justify="right")
    table.add_column("Alpha", justify="right")
    table.add_column("Sharpe", justify="right")
    table.add_column("MaxDD", justify="right")
    table.add_column("Trades", justify="right")
    table.add_column("Win%", justify="right")
    table.add_column("Exp%", justify="right")

    shown = result.windows[:max_rows]
    for w in shown:
        ret_color = "green" if w.total_return > 0 else "red"
        alpha_color = "green" if w.alpha > 0 else "red"
        table.add_row(
            f"{w.window_start} → {w.window_end}",
            f"[{ret_color}]{_pct(w.total_return)}[/{ret_color}]",
            _pct(w.benchmark_return),
            f"[{alpha_color}]{_pct(w.alpha)}[/{alpha_color}]",
            f"{w.sharpe:.2f}",
            _pct(w.max_drawdown),
            str(w.num_trades),
            _pct(w.win_rate),
            _pct(w.avg_exposure),
        )
    if len(result.windows) > max_rows:
        table.add_row(f"… {len(result.windows) - max_rows} more", "", "", "", "", "", "", "", "")
    console.print(table)

    worst = s.get("worst_window", ["", "", 0.0])
    summary_lines = (
        f"[bold]Windows:[/] {s.get('n_windows', 0)}    "
        f"[bold]Profitable:[/] {_pct(s.get('pct_profitable', 0.0))}\n"
        f"[bold]Return[/]  mean {_pct(s.get('mean_return', 0.0))} · "
        f"median {_pct(s.get('median_return', 0.0))} · "
        f"min {_pct(s.get('min_return', 0.0))} · max {_pct(s.get('max_return', 0.0))} · "
        f"std {_pct(s.get('std_return', 0.0))}\n"
        f"[bold]Sharpe[/]  mean {s.get('mean_sharpe', 0.0):.2f} · "
        f"median {s.get('median_sharpe', 0.0):.2f} · "
        f"min {s.get('min_sharpe', 0.0):.2f} · max {s.get('max_sharpe', 0.0):.2f}\n"
        f"[bold]Benchmark[/]  mean {_pct(s.get('mean_benchmark', 0.0))} (equal-weight B&H)\n"
        f"[bold]Alpha[/]  mean {_pct(s.get('mean_alpha', 0.0))} · "
        f"median {_pct(s.get('median_alpha', 0.0))} · "
        f"positive {_pct(s.get('pct_positive_alpha', 0.0))} of windows\n"
        f"[bold]Exposure[/]  mean {_pct(s.get('mean_exposure', 0.0))} of capital deployed per window\n"
        f"[bold]Worst window:[/] {worst[0]} → {worst[1]} ({_pct(worst[2])})"
    )
    console.print(Panel(summary_lines, title="Robustness summary", border_style="cyan"))


def _params_str(params: dict) -> str:  # type: ignore[type-arg]
    """Compact param rendering, e.g. 'adx_max=25 z_entry=1.5' (ints shown without .0)."""
    def fmt(v: object) -> str:
        return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)
    return " ".join(f"{k}={fmt(v)}" for k, v in sorted(params.items())) or "(defaults)"


def render_sweep(result: SweepResult, console: Console | None = None, max_rows: int = 25) -> None:
    """Render a parameter-sweep comparison: configs ranked by the chosen metric, plus a
    rolling-window stability cross-check for the top configs (the anti-overfitting view)."""
    console = console or Console()
    console.print(
        f"\n[bold cyan]Parameter sweep: {result.strategy_name}[/]  "
        f"[dim]({len(result.rows)} configs, ranked by {result.rank_by})[/]\n"
    )
    if not result.rows:
        console.print("[yellow]No configurations evaluated.[/]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", justify="right")
    table.add_column("Params")
    table.add_column("Sharpe", justify="right")
    table.add_column("Return", justify="right")
    table.add_column("MaxDD", justify="right")
    table.add_column("PF", justify="right")
    table.add_column("Win%", justify="right")
    table.add_column("Trades", justify="right")
    table.add_column("Exp%", justify="right")

    for row in result.rows[:max_rows]:
        m = row.metrics
        pf = m.get("profit_factor", 0.0)
        pf_str = "∞" if pf == float("inf") else f"{pf:.2f}"
        style = "bold green" if row.rank == 1 else ""
        ret_color = "green" if m.get("total_return", 0) > 0 else "red"
        table.add_row(
            f"[{style}]{row.rank}[/{style}]" if style else str(row.rank),
            _params_str(row.params),
            f"{m.get('sharpe', 0):.2f}",
            f"[{ret_color}]{_pct(m.get('total_return', 0))}[/{ret_color}]",
            _pct(m.get("max_drawdown", 0)),
            pf_str,
            _pct(m.get("win_rate", 0)),
            str(m.get("num_trades", 0)),
            _pct(m.get("avg_exposure", 0)),
        )
    if len(result.rows) > max_rows:
        table.add_row(f"… {len(result.rows) - max_rows} more", "", "", "", "", "", "", "", "")
    console.print(table)

    if result.stability:
        stab = Table(show_header=True, header_style="bold", title="Stability cross-check (rolling windows)")
        stab.add_column("Params")
        stab.add_column("Mean Sharpe", justify="right")
        stab.add_column("Profitable%", justify="right")
        stab.add_column("Mean Alpha", justify="right")
        stab.add_column("PosAlpha%", justify="right")
        for srow in result.stability:
            s = srow.rolling_summary
            stab.add_row(
                _params_str(srow.params),
                f"{s.get('mean_sharpe', 0.0):.2f}",
                _pct(s.get("pct_profitable", 0.0)),
                _pct(s.get("mean_alpha", 0.0)),
                _pct(s.get("pct_positive_alpha", 0.0)),
            )
        console.print(stab)
        console.print(
            "[dim]A top config that is also stable here (consistently profitable / positive "
            "Sharpe across windows) is trustworthy; one that wins above but is weak here is overfit.[/]"
        )


def render_combined_report(
    standalone: "_StandaloneList",
    combined: BacktestResult,
    benchmark_return: float,
    console: Console | None = None,
) -> None:
    """Side-by-side comparison: each strategy standalone + combined allocation.

    `standalone` is a list of (display_name, BacktestResult) pairs — one per sub-strategy.
    `benchmark_return` is the equal-weight buy-and-hold return over the same period/universe
    (use walkforward._benchmark_return(bars) to compute it).
    """
    console = console or Console()
    cfg = combined.config

    console.print(f"\n[bold cyan]Combined Allocation: {cfg.strategy_name}[/]")
    console.print(
        f"[dim]{cfg.from_date} → {cfg.to_date} · {cfg.interval} · "
        f"{len(cfg.symbols)} symbols · capital ₹{cfg.initial_capital:,.0f} · "
        f"fees={'on' if cfg.fees_enabled else 'OFF'} · slippage={cfg.slippage_bps}bps · "
        f"benchmark (equal-weight B&H) {_pct(benchmark_return)}[/]\n"
    )

    all_cols: list[tuple[str, dict]] = [  # type: ignore[type-arg]
        (name, res.metrics) for name, res in standalone
    ]
    all_cols.append(("combined", combined.metrics))

    table = Table(show_header=True, header_style="bold")
    table.add_column("Metric")
    for col_name, _ in all_cols:
        style = "bold" if col_name == "combined" else ""
        table.add_column(col_name, justify="right", style=style)

    metric_rows = [
        ("total_return", "Total Return", _pct),
        ("cagr", "CAGR", _pct),
        ("sharpe", "Sharpe", lambda x: f"{x:.2f}"),
        ("sortino", "Sortino", lambda x: f"{x:.2f}"),
        ("max_drawdown", "Max Drawdown", _pct),
        ("profit_factor", "Profit Factor",
            lambda x: "∞" if x == float("inf") else f"{x:.2f}"),
        ("win_rate", "Win Rate", _pct),
        ("num_trades", "Trades", lambda x: str(int(x))),
        ("avg_exposure", "Avg Exposure", _pct),
        ("final_equity", "Final Equity", lambda x: f"₹{x:,.0f}"),
    ]
    for key, label, fmt in metric_rows:
        row = [label]
        for _, m in all_cols:
            row.append(fmt(m.get(key, 0.0)))  # type: ignore[arg-type]
        table.add_row(*row)

    # Alpha vs. benchmark row
    alpha_row = ["Alpha vs B&H"]
    for _, m in all_cols:
        a = m.get("total_return", 0.0) - benchmark_return
        color = "green" if a > 0 else "red"
        alpha_row.append(f"[{color}]{_pct(a)}[/{color}]")
    table.add_row(*alpha_row)

    console.print(table)

    # Diversification summary panel
    s_sharpes = [m.get("sharpe", 0.0) for _, m in all_cols[:-1]]
    c_sharpe = combined.metrics.get("sharpe", 0.0)
    best_sharpe = max(s_sharpes) if s_sharpes else 0.0
    sharpe_delta = c_sharpe - best_sharpe

    s_dds = [m.get("max_drawdown", 0.0) for _, m in all_cols[:-1]]
    c_dd = combined.metrics.get("max_drawdown", 0.0)
    # max_drawdown is negative; max() gives the least-negative (best DD)
    best_dd = max(s_dds) if s_dds else 0.0
    dd_delta = c_dd - best_dd  # negative = combined DD is worse

    sd_color = "green" if sharpe_delta > 0 else "red"
    dd_color = "green" if dd_delta >= 0 else "red"

    combined_alpha = combined.metrics.get("total_return", 0.0) - benchmark_return
    ca_color = "green" if combined_alpha > 0 else "red"

    panel_text = (
        f"[bold]Combined Sharpe:[/] {c_sharpe:.2f}  "
        f"[bold]Best standalone:[/] {best_sharpe:.2f}  "
        f"[bold]Δ Sharpe:[/] [{sd_color}]{sharpe_delta:+.2f}[/{sd_color}]\n"
        f"[bold]Combined Max DD:[/] {_pct(c_dd)}  "
        f"[bold]Best standalone DD:[/] {_pct(best_dd)}  "
        f"[bold]Δ DD:[/] [{dd_color}]{_pct(dd_delta)}[/{dd_color}]\n"
        f"[bold]Combined Alpha:[/] [{ca_color}]{_pct(combined_alpha)}[/{ca_color}]  "
        f"vs. equal-weight B&H {_pct(benchmark_return)}\n"
        f"[dim]Positive Δ Sharpe = decorrelation benefit; "
        f"positive Δ DD = lower max drawdown in the combined book.[/]"
    )
    console.print(Panel(panel_text, title="Diversification summary", border_style="cyan"))
