"""Backtester agent entrypoint.

Modes:
  forward  (default) — long-running paper-test: consume live bars off the bus and push them
                       through a persistent BacktestEngine (simulated fills/fees; no real orders).
  batch              — one-shot historic backtest from the DB (delegates to `trading backtest`).

Run as a container:
  python -m trading_system.agents.backtester.app --strategy momentum --interval day

Note (skeleton): the Reader publishes OHLCV bars without computed features, so feature-dependent
strategies won't generate signals on the live stream yet — attaching live features is Phase 4b.
The forward *engine path* is proven by tests/test_forward.py (equivalence to a batch run).
"""
import argparse
import signal
import sys
from datetime import date

from trading_system.agents.backtester.forward import ForwardTester
from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.results import BacktestConfig
from trading_system.bus.redis_streams import RedisBarBus
from trading_system.config import get_settings
from trading_system.execution.fees import Product
from trading_system.observability import get_logger, setup_logging
from trading_system.strategy.registry import get_strategy, list_strategies

logger = get_logger(__name__)


def _parse_params(items: list[str] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in items or []:
        key, _, val = item.partition("=")
        out[key.strip()] = float(val)
    return out


def run_forward(args: argparse.Namespace) -> int:
    s = get_settings()
    symbols = args.symbols or s.liquid_symbols
    params = _parse_params(args.param)

    try:
        strategy = get_strategy(args.strategy, **params)
    except KeyError:
        logger.error("unknown_strategy", strategy=args.strategy, available=list_strategies())
        return 1

    # Intraday strategies (Phase 4b) trade MIS; daily/overnight strategies trade CNC delivery.
    product = Product.MIS if getattr(strategy, "intraday", False) else Product.CNC

    config = BacktestConfig(
        strategy_name=strategy.name, strategy_version=strategy.version, params=strategy.params,
        symbols=symbols, interval=args.interval,
        from_date=date.today(), to_date=date.today(),
        initial_capital=s.backtest_initial_capital, slippage_bps=s.backtest_slippage_bps,
        fees_enabled=True, feature_set_version=args.feature_version,
    )
    engine = BacktestEngine(strategy, config, product=product)
    bus = RedisBarBus()
    tester = ForwardTester(
        engine, bus,
        group=args.group, consumer=args.consumer,
        interval=args.interval, symbols=symbols,
        start_id="$",  # live-only: ignore bars published before this consumer started
    )

    stop = {"flag": False}

    def _handle(signum: int, _frame: object) -> None:
        logger.info("forward_shutdown_signal", signum=signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    logger.info(
        "forward_start",
        strategy=strategy.name, interval=args.interval, symbols=len(symbols),
        product=product.value, group=args.group,
    )
    try:
        tester.run_until(lambda: stop["flag"])
    finally:
        result = tester.result()
        logger.info(
            "forward_done",
            bars_fed=tester.fed,
            trades=len(result.trades),
            final_equity=round(engine.portfolio.equity(), 2),
        )
    return 0


def run_batch(args: argparse.Namespace) -> int:
    # Historic batch backtests are the existing CLI path; keep one implementation.
    logger.info(
        "batch_delegate",
        hint=f"Run: trading backtest --strategy {args.strategy} "
        f"--interval {args.interval} --from <d> --to <d>",
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Backtester agent (forward paper-test / batch).")
    p.add_argument("--mode", choices=["forward", "batch"], default="forward")
    p.add_argument("--strategy", default="momentum")
    p.add_argument("--interval", default="day")
    p.add_argument("--symbols", nargs="*", default=None, help="default: settings.liquid_symbols")
    p.add_argument("--feature-version", default="v1")
    p.add_argument("--param", action="append", help="strategy param override, e.g. size_hint=0.1")
    p.add_argument("--group", default="backtester")
    p.add_argument("--consumer", default="backtester-1")
    return p


def main(argv: list[str] | None = None) -> int:
    setup_logging(get_settings().log_level)
    args = build_parser().parse_args(argv)
    return run_forward(args) if args.mode == "forward" else run_batch(args)


if __name__ == "__main__":
    sys.exit(main())
