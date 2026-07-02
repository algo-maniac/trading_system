"""Session-aware intraday features: VWAP/OR reset per session, no lookahead, ADX bounded."""
import numpy as np
import pandas as pd
import pytest

from trading_system.features.engineering import compute_intraday_features


def _session(day: int, base: float, n: int = 75) -> pd.DataFrame:
    """One NSE 5-min session (09:15 + n bars), naive IST."""
    ts = pd.date_range(f"2024-01-0{day} 09:15", periods=n, freq="5min")
    rng = np.random.default_rng(day)
    close = base + np.cumsum(rng.standard_normal(n))
    open_ = close + rng.standard_normal(n) * 0.2
    high = np.maximum(open_, close) + np.abs(rng.standard_normal(n)) * 0.3
    low = np.minimum(open_, close) - np.abs(rng.standard_normal(n)) * 0.3
    vol = rng.integers(1000, 5000, n)
    return pd.DataFrame({"ts": ts, "open": open_, "high": high, "low": low, "close": close, "volume": vol})


def _two_sessions() -> pd.DataFrame:
    return pd.concat([_session(2, 100.0), _session(3, 200.0)], ignore_index=True)


def test_vwap_resets_each_session():
    df = _two_sessions()
    out = compute_intraday_features(df)
    # First bar of each session: VWAP == that bar's typical price (cumulative starts fresh).
    for start in (0, 75):
        typ = (df.high[start] + df.low[start] + df.close[start]) / 3
        assert out["vwap"].iloc[start] == pytest.approx(typ)


def test_vwap_matches_cumulative_formula_within_session():
    df = _two_sessions()
    out = compute_intraday_features(df)
    t = 40
    tpv = ((df.high[: t + 1] + df.low[: t + 1] + df.close[: t + 1]) / 3 * df.volume[: t + 1]).sum()
    cv = df.volume[: t + 1].sum()
    assert out["vwap"].iloc[t] == pytest.approx(tpv / cv)


def test_opening_range_constant_after_window_and_independent_per_session():
    df = _two_sessions()
    out = compute_intraday_features(df)
    s1 = out.iloc[0:75]
    after = s1[s1["minutes_since_open"] >= 15]["or_high"]
    assert after.nunique() == 1                       # constant once OR has formed
    assert after.iloc[0] == df.high[0:3].max()        # = max high of first 3 (5-min) bars
    s2_or = out.iloc[75:150]["or_high"].dropna().iloc[-1]
    assert s2_or != after.iloc[0]                     # each session has its own OR


def test_time_of_day_and_session_return_reset():
    df = _two_sessions()
    out = compute_intraday_features(df)
    assert out["minutes_since_open"].iloc[75] == 0.0
    assert out["bar_in_session"].iloc[75] == 0.0
    assert out["session_return"].iloc[0] == df.close[0] / df.open[0] - 1.0


def test_adx_bounded_and_rel_volume_positive():
    df = _two_sessions()
    out = compute_intraday_features(df)
    adx = out["adx_14"].dropna()
    assert not adx.empty
    assert (adx >= 0).all() and (adx <= 100).all()
    assert (out["rel_volume"].dropna() > 0).all()


def test_gap_pct_is_open_vs_prior_session_close_and_constant():
    df = _two_sessions()
    out = compute_intraday_features(df)
    # first session has no prior close → NaN throughout
    assert out["gap_pct"].iloc[:75].isna().all()
    # second session: open of its first bar vs close of prior session's last bar
    expected = df.open[75] / df.close[74] - 1.0
    assert out["gap_pct"].iloc[75] == pytest.approx(expected)
    # constant across the whole session (known at open, no lookahead)
    assert (out["gap_pct"].iloc[75:150] == out["gap_pct"].iloc[75]).all()
