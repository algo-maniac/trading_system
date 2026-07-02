"""Rolling-window robustness tests (no DB — ReplayEngine is monkeypatched)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from trading_system.analytics import walkforward
from trading_system.analytics.walkforward import (
    WindowResult,
    _benchmark_return,
    _check_feature_warmup,
    _summarize,
    generate_windows,
    run_rolling,
)
from trading_system.replay.events import BarEvent

_UTC = timezone.utc


# --- generate_windows ---

def test_generate_windows_spans_and_step():
    w = generate_windows(date(2024, 1, 1), date(2024, 4, 10), window_days=30, step_days=30)
    assert w[0] == (date(2024, 1, 1), date(2024, 1, 30))    # 30 calendar days inclusive
    assert w[1][0] == date(2024, 1, 31)                      # stepped by 30
    assert all((e - s).days == 29 for s, e in w)             # every window same length
    assert all(e <= date(2024, 4, 10) for _, e in w)         # never exceeds to_date


def test_generate_windows_overlapping_step():
    w = generate_windows(date(2024, 1, 1), date(2024, 3, 1), window_days=30, step_days=10)
    assert w[0] == (date(2024, 1, 1), date(2024, 1, 30))
    assert w[1] == (date(2024, 1, 11), date(2024, 2, 9))     # overlaps the first


def test_generate_windows_range_shorter_than_window():
    assert generate_windows(date(2024, 1, 1), date(2024, 1, 20), 30, 10) == []


def test_generate_windows_rejects_nonpositive():
    with pytest.raises(ValueError):
        generate_windows(date(2024, 1, 1), date(2024, 2, 1), 0, 10)
    with pytest.raises(ValueError):
        generate_windows(date(2024, 1, 1), date(2024, 2, 1), 30, 0)


# --- _summarize ---

def test_summarize_aggregates():
    ws = [
        WindowResult(date(2024, 1, 1), date(2024, 1, 30), 0.10, 1.0, -0.05, 3, 0.6),
        WindowResult(date(2024, 2, 1), date(2024, 3, 1), -0.04, -0.5, -0.08, 2, 0.5),
        WindowResult(date(2024, 3, 2), date(2024, 3, 31), 0.02, 0.3, -0.03, 1, 1.0),
    ]
    s = _summarize(ws)
    assert s["n_windows"] == 3
    assert s["pct_profitable"] == pytest.approx(2 / 3)
    assert s["max_return"] == pytest.approx(0.10)
    assert s["min_return"] == pytest.approx(-0.04)
    assert s["worst_window"][2] == pytest.approx(-0.04)
    assert s["best_window"][2] == pytest.approx(0.10)


def test_summarize_empty():
    assert _summarize([]) == {"n_windows": 0}


# --- run_rolling (monkeypatched bar source) ---

def _series():
    """90 daily bars; an oversold dip around day 20 triggers one mean-reversion trade."""
    bars = []
    start = datetime(2024, 1, 2, tzinfo=_UTC)
    for i in range(90):
        ts = start + timedelta(days=i)
        if i == 20:
            feats, close = {"rsi_14": 15.0, "sma_20": 100.0}, 90.0   # oversold → BUY
        else:
            feats, close = {"rsi_14": 50.0, "sma_20": 100.0}, 100.0
        bars.append(BarEvent(
            ts=ts, symbol="AAA", interval="day", instrument_token=1,
            open=close, high=close, low=close, close=close, volume=100_000, features=feats,
        ))
    return bars


@pytest.fixture
def _patched_replay(monkeypatch):
    bars = _series()

    class _FakeReplay:
        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            return iter(list(bars))

    monkeypatch.setattr(walkforward, "ReplayEngine", _FakeReplay)


def _run():
    return run_rolling(
        strategy_name="mean_reversion", params={}, symbols=["AAA"], token_map={"AAA": 1},
        interval="day", from_date=date(2024, 1, 2), to_date=date(2024, 3, 31),
        window_days=40, step_days=20, initial_capital=1_000_000,
        slippage_bps=5.0, fees_enabled=False, feature_set_version="v1",
    )


def test_run_rolling_window_count(_patched_replay):
    result = _run()
    expected = generate_windows(date(2024, 1, 2), date(2024, 3, 31), 40, 20)
    assert len(result.windows) == len(expected)
    assert result.strategy_version == "v1"
    assert result.summary["n_windows"] == len(expected)


def test_run_rolling_executes_engine(_patched_replay):
    result = _run()
    # At least one window must span the dip and book the round-trip — proves the
    # engine actually ran over the sliced bars, not just produced empty windows.
    assert any(w.num_trades >= 1 for w in result.windows)


def _window_key(result):
    return [
        (w.window_start, w.window_end, w.total_return, w.sharpe, w.num_trades)
        for w in result.windows
    ]


def test_run_rolling_deterministic(_patched_replay):
    r1, r2 = _run(), _run()
    assert _window_key(r1) == _window_key(r2)
    assert r1.summary == r2.summary


# --- _check_feature_warmup ---

def _make_bar(day_offset: int, features: dict) -> BarEvent:  # type: ignore[type-arg]
    ts = datetime(2024, 1, 2, tzinfo=_UTC) + timedelta(days=day_offset)
    return BarEvent(
        ts=ts, symbol="X", interval="day", instrument_token=1,
        open=100.0, high=100.0, low=100.0, close=100.0, volume=1000,
        features=features,
    )


def test_warmup_no_warning_when_all_features_present(capsys):
    bars = [_make_bar(i, {"sma_200": 100.0}) for i in range(30)]
    windows = generate_windows(date(2024, 1, 2), date(2024, 3, 1), 20, 10)
    _check_feature_warmup(bars, windows, "test_strat")
    assert "feature_warmup_gap" not in capsys.readouterr().out


def test_warmup_warns_when_feature_missing_in_leading_bars(capsys):
    # First 25 bars: sma_200 absent (warmup). Last 5: present.
    bars = (
        [_make_bar(i, {}) for i in range(25)]
        + [_make_bar(i, {"sma_200": 100.0}) for i in range(25, 30)]
    )
    windows = generate_windows(date(2024, 1, 2), date(2024, 3, 1), 20, 10)
    _check_feature_warmup(bars, windows, "momentum_v2")
    out = capsys.readouterr().out
    assert "feature_warmup_gap" in out
    assert "sma_200" in out
    assert "rolling_warmup_bias" in out


def test_warmup_no_warning_when_null_fraction_below_threshold(capsys):
    # Only 2 out of 100 bars missing — below the 5% threshold.
    bars = (
        [_make_bar(i, {}) for i in range(2)]
        + [_make_bar(i, {"sma_200": 100.0}) for i in range(2, 100)]
    )
    windows = generate_windows(date(2024, 1, 2), date(2024, 6, 1), 20, 10)
    _check_feature_warmup(bars, windows, "test_strat")
    assert "feature_warmup_gap" not in capsys.readouterr().out


def test_warmup_no_warning_when_no_windows_affected(capsys):
    # Feature absent only in bars that precede all window start dates.
    bars = (
        [_make_bar(i, {}) for i in range(5)]
        + [_make_bar(i, {"sma_200": 100.0}) for i in range(5, 35)]
    )
    windows = [(date(2024, 2, 1), date(2024, 2, 20))]
    _check_feature_warmup(bars, windows, "test_strat")
    assert "feature_warmup_gap" not in capsys.readouterr().out


# --- _benchmark_return ---

def _make_bar_sym(day_offset: int, symbol: str, close: float) -> BarEvent:
    ts = datetime(2024, 1, 2, tzinfo=_UTC) + timedelta(days=day_offset)
    return BarEvent(
        ts=ts, symbol=symbol, interval="day", instrument_token=1,
        open=close, high=close, low=close, close=close, volume=1000,
        features={},
    )


def test_benchmark_return_equal_weight_two_symbols():
    # X: 100→110 = +10%; Y: 200→180 = −10%; equal-weight mean = 0%
    bars = [
        _make_bar_sym(0, "X", 100.0),
        _make_bar_sym(1, "X", 105.0),
        _make_bar_sym(2, "X", 110.0),
        _make_bar_sym(0, "Y", 200.0),
        _make_bar_sym(1, "Y", 190.0),
        _make_bar_sym(2, "Y", 180.0),
    ]
    assert _benchmark_return(bars) == pytest.approx(0.0)


def test_benchmark_return_single_symbol():
    # X: 100→120 = +20%
    bars = [_make_bar_sym(0, "X", 100.0), _make_bar_sym(1, "X", 120.0)]
    assert _benchmark_return(bars) == pytest.approx(0.20)


def test_benchmark_return_empty():
    assert _benchmark_return([]) == pytest.approx(0.0)


def test_benchmark_return_single_bar_per_symbol_excluded():
    # Only one bar per symbol — needs ≥2 bars to compute a return
    bars = [_make_bar_sym(0, "X", 100.0), _make_bar_sym(0, "Y", 200.0)]
    assert _benchmark_return(bars) == pytest.approx(0.0)


def test_alpha_equals_return_minus_benchmark(_patched_replay):
    result = _run()
    for w in result.windows:
        assert w.alpha == pytest.approx(w.total_return - w.benchmark_return, abs=1e-9)
        assert 0.0 <= w.avg_exposure <= 1.0
    assert "mean_alpha" in result.summary
    assert "median_alpha" in result.summary
    assert "pct_positive_alpha" in result.summary
    assert "mean_benchmark" in result.summary
    assert "mean_exposure" in result.summary
