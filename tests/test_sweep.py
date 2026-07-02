"""Parameter sweep: cartesian grid, Sharpe-ranking, determinism, stability cross-check hook."""
from datetime import date

from tests.conftest import make_bar
from trading_system.analytics.sweep import _grid_combos, run_sweep
from trading_system.backtest.results import BacktestConfig


def _stream():
    return [
        make_bar(1, "AAA", close=100, open_=100, features={"sma_20": 95, "sma_50": 100, "momentum_20": 0.01}),
        make_bar(2, "AAA", close=102, open_=101, features={"sma_20": 105, "sma_50": 100, "momentum_20": 0.05}),
        make_bar(3, "AAA", close=103, open_=102, high=104, low=101, features={"sma_20": 106, "sma_50": 100, "momentum_20": 0.05}),
        make_bar(4, "AAA", close=104, open_=103, high=105, low=102, features={"sma_20": 95, "sma_50": 100, "momentum_20": -0.01}),
        make_bar(5, "AAA", close=104, open_=104, high=105, low=103, features={"sma_20": 95, "sma_50": 100, "momentum_20": -0.01}),
    ]


def _base():
    return BacktestConfig(
        strategy_name="momentum", strategy_version="v1", params={}, symbols=["AAA"],
        interval="day", from_date=date(2024, 1, 1), to_date=date(2024, 1, 5),
        initial_capital=1_000_000, slippage_bps=0.0, fees_enabled=True, feature_set_version="v1",
    )


def test_grid_cartesian_product_count_and_order():
    combos = _grid_combos({"a": [1, 2], "b": [10, 20, 30]})
    assert len(combos) == 6                       # 2 × 3
    assert combos[0] == {"a": 1, "b": 10}          # sorted axis names, first product
    assert all(set(c) == {"a", "b"} for c in combos)


def test_sweep_ranks_descending_by_sharpe():
    res = run_sweep("momentum", {"size_hint": [0.1, 0.2], "stop_pct": [0.05, 0.08]}, _base(), _stream())
    assert len(res.rows) == 4
    sharpes = [r.metrics.get("sharpe", 0) for r in res.rows]
    assert sharpes == sorted(sharpes, reverse=True)
    assert res.rows[0].rank == 1 and res.rows[-1].rank == 4


def test_sweep_rank_by_switch():
    res = run_sweep("momentum", {"size_hint": [0.1, 0.2]}, _base(), _stream(), rank_by="total_return")
    rets = [r.metrics.get("total_return", 0) for r in res.rows]
    assert rets == sorted(rets, reverse=True)


def test_sweep_determinism():
    grid = {"size_hint": [0.1, 0.2], "stop_pct": [0.05, 0.08]}
    r1 = run_sweep("momentum", grid, _base(), _stream())
    r2 = run_sweep("momentum", grid, _base(), _stream())
    assert [(r.params, r.rank) for r in r1.rows] == [(r.params, r.rank) for r in r2.rows]


def test_sweep_stability_cross_check_invoked_for_top_k():
    calls = []

    def fake_stability(params):
        calls.append(params)
        return {"mean_sharpe": 1.0, "pct_profitable": 0.8, "mean_alpha": 0.01, "pct_positive_alpha": 0.7}

    res = run_sweep(
        "momentum", {"size_hint": [0.1, 0.2, 0.3]}, _base(), _stream(),
        top_k=2, stability_fn=fake_stability,
    )
    assert len(res.stability) == 2                # only top-K cross-checked
    assert len(calls) == 2
    assert res.stability[0].rolling_summary["pct_profitable"] == 0.8
