"""Unit tests for feature engineering — deterministic, no DB required."""
import numpy as np
import pandas as pd
import pytest

from tests.conftest import make_ohlcv_df
from trading_system.features.engineering import compute_features


def test_all_feature_columns_present():
    df = make_ohlcv_df(100)
    out = compute_features(df)
    expected = [
        "returns_1", "returns_5", "log_returns_1",
        "momentum_5", "momentum_10", "momentum_20",
        "volatility_10", "volatility_20",
        "rsi_14", "macd_line", "macd_signal", "macd_hist",
        "ema_12", "ema_26", "sma_20", "sma_50",
        "volume_zscore_20",
    ]
    for col in expected:
        assert col in out.columns, f"Missing feature column: {col}"


def test_returns_1_matches_pct_change():
    df = make_ohlcv_df(50)
    out = compute_features(df)
    expected = df["close"].astype(float).pct_change(1)
    pd.testing.assert_series_equal(
        out["returns_1"].reset_index(drop=True),
        expected.reset_index(drop=True),
        check_names=False,
    )


def test_warmup_nan_sma50_short_series():
    df = make_ohlcv_df(49)  # fewer bars than SMA-50 window
    out = compute_features(df)
    assert out["sma_50"].isna().all(), "SMA-50 should be all NaN with <50 bars"


def test_warmup_nan_sma20():
    df = make_ohlcv_df(19)
    out = compute_features(df)
    assert out["sma_20"].isna().all()


def test_rsi_bounded_0_to_100():
    df = make_ohlcv_df(100)
    out = compute_features(df)
    rsi = out["rsi_14"].dropna()
    assert not rsi.empty
    assert (rsi >= 0).all() and (rsi <= 100).all()


def test_macd_hist_equals_line_minus_signal():
    df = make_ohlcv_df(100)
    out = compute_features(df)
    diff = (out["macd_line"] - out["macd_signal"]).round(10)
    hist = out["macd_hist"].round(10)
    mask = diff.notna() & hist.notna()
    pd.testing.assert_series_equal(diff[mask].reset_index(drop=True), hist[mask].reset_index(drop=True), check_names=False)


def test_log_returns_relation_to_returns():
    df = make_ohlcv_df(50)
    out = compute_features(df)
    # log_returns_1 should be close to returns_1 for small moves
    lr = out["log_returns_1"].dropna()
    r1 = out["returns_1"].dropna()
    # Pearson correlation should be very high
    corr = lr.corr(r1)
    assert corr > 0.999


def test_same_input_same_output_determinism():
    df = make_ohlcv_df(100, seed=7)
    out1 = compute_features(df.copy())
    out2 = compute_features(df.copy())
    pd.testing.assert_frame_equal(out1, out2)


def test_volume_zscore_mean_near_zero():
    df = make_ohlcv_df(200, seed=99)
    out = compute_features(df)
    zscore = out["volume_zscore_20"].dropna()
    # Mean of z-scores should be close to 0
    assert abs(zscore.mean()) < 0.5


def test_momentum_252_21_value_and_warmup():
    # 300 daily bars with a known close path → check the 12-1 month momentum formula.
    df = make_ohlcv_df(300, freq="B", interval="day")
    out = compute_features(df)
    close = df["close"].astype(float).reset_index(drop=True)
    # NaN for the first 252 bars (needs close.shift(252)); present afterwards.
    assert out["momentum_252_21"].iloc[:252].isna().all()
    assert out["momentum_252_21"].iloc[252:].notna().all()
    # Definition: close[t-21] / close[t-252] - 1.0
    t = 270
    expected = close.iloc[t - 21] / close.iloc[t - 252] - 1.0
    assert out["momentum_252_21"].iloc[t] == pytest.approx(expected)
