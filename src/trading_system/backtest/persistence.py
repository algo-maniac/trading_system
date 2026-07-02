"""Persist backtest results to TimescaleDB for audit and comparison."""
import json

import psycopg

from trading_system.backtest.results import BacktestResult
from trading_system.data.storage import get_connection
from trading_system.observability import get_logger

logger = get_logger(__name__)


def save_result(result: BacktestResult) -> int:
    """Write run + trades + equity curve + signal audit. Returns the run_id."""
    cfg = result.config
    with get_connection() as conn:
        run_id = _insert_run(conn, result)
        _insert_trades(conn, run_id, result)
        _insert_equity(conn, run_id, result)
        _insert_signals(conn, run_id, result)
        conn.commit()
    logger.info("backtest_persisted", run_id=run_id, strategy=cfg.strategy_name)
    return run_id


def _insert_run(conn: psycopg.Connection, result: BacktestResult) -> int:  # type: ignore[type-arg]
    cfg = result.config
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO backtest_runs (
                strategy_name, strategy_version, params, universe, interval,
                from_date, to_date, initial_capital, fees_enabled, slippage_bps,
                config_hash, metrics
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                cfg.strategy_name, cfg.strategy_version, json.dumps(cfg.params),
                cfg.symbols, cfg.interval, cfg.from_date, cfg.to_date,
                cfg.initial_capital, cfg.fees_enabled, cfg.slippage_bps,
                cfg.config_hash(), json.dumps(result.metrics),
            ),
        )
        row = cur.fetchone()
        return int(row[0])  # type: ignore[index]


def _insert_trades(conn: psycopg.Connection, run_id: int, result: BacktestResult) -> None:  # type: ignore[type-arg]
    if not result.trades:
        return
    rows = [
        (
            run_id, t.symbol, t.side, t.entry_ts, t.entry_price, t.exit_ts, t.exit_price,
            t.qty, t.fees, t.slippage, t.pnl, t.return_pct, t.exit_reason,
        )
        for t in result.trades
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO backtest_trades (
                run_id, symbol, side, entry_ts, entry_price, exit_ts, exit_price,
                qty, fees, slippage, pnl, return_pct, exit_reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            rows,
        )


def _insert_equity(conn: psycopg.Connection, run_id: int, result: BacktestResult) -> None:  # type: ignore[type-arg]
    if not result.equity_curve:
        return
    rows = [
        (run_id, p.ts, p.equity, p.cash, p.positions_value, p.drawdown)
        for p in result.equity_curve
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO backtest_equity (run_id, ts, equity, cash, positions_value, drawdown)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, ts) DO NOTHING
            """,
            rows,
        )


def _insert_signals(conn: psycopg.Connection, run_id: int, result: BacktestResult) -> None:  # type: ignore[type-arg]
    if not result.signals:
        return
    rows = [
        (run_id, s.ts, s.symbol, s.side, s.risk_action, s.approved_qty, s.reason)
        for s in result.signals
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO backtest_signals (run_id, ts, symbol, side, risk_action, approved_qty, reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            rows,
        )


def save_rolling_result(result) -> int:  # type: ignore[no-untyped-def]
    """Persist a RollingResult: one rolling_runs row + per-window rolling_windows rows.

    Returns the rolling_run_id. Typed loosely to avoid importing analytics at module load.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rolling_runs (
                    strategy_name, strategy_version, params, universe, interval,
                    from_date, to_date, window_days, step_days, summary
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    result.strategy_name, result.strategy_version, json.dumps(result.params),
                    result.symbols, result.interval, result.from_date, result.to_date,
                    result.window_days, result.step_days, json.dumps(result.summary),
                ),
            )
            rolling_run_id = int(cur.fetchone()[0])  # type: ignore[index]

            if result.windows:
                rows = [
                    (
                        rolling_run_id, w.window_start, w.window_end,
                        json.dumps({
                            "total_return": w.total_return, "sharpe": w.sharpe,
                            "max_drawdown": w.max_drawdown, "num_trades": w.num_trades,
                            "win_rate": w.win_rate,
                            "benchmark_return": w.benchmark_return,
                            "alpha": w.alpha,
                            "avg_exposure": w.avg_exposure,
                        }),
                    )
                    for w in result.windows
                ]
                cur.executemany(
                    """
                    INSERT INTO rolling_windows (rolling_run_id, window_start, window_end, metrics)
                    VALUES (%s, %s, %s, %s)
                    """,
                    rows,
                )
        conn.commit()
    logger.info("rolling_persisted", rolling_run_id=rolling_run_id, strategy=result.strategy_name)
    return rolling_run_id
