"""Technical indicator computations — all hand-implemented on numpy/pandas.

No external TA library dependency: full determinism control, no version-drift risk.
NaN is returned for warmup bars; values are never forward-filled to avoid lookahead.

Input contract: df must have columns ts, open, high, low, close, volume,
sorted ascending by ts (oldest first).
"""
import numpy as np
import pandas as pd


def _ema(series: pd.Series, span: int) -> pd.Series:
    """Exponential moving average. Returns NaN until `span` bars have been seen."""
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def _sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI using EWM smoothing (com = period-1, adjust=False)."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range via Wilder smoothing. NaN until `period` bars are seen."""
    prev_close = close.shift(1)
    true_range = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return true_range.ewm(com=period - 1, adjust=False, min_periods=period).mean()


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average Directional Index (Wilder) — trend-strength regime filter, bounded 0..100.

    High ADX ⇒ trending (favors breakout); low ADX ⇒ range-bound (favors mean-reversion).
    """
    up = high.diff()
    down = -low.diff()
    plus_dm = ((up > down) & (up > 0)) * up.clip(lower=0)
    minus_dm = ((down > up) & (down > 0)) * down.clip(lower=0)
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = tr.ewm(com=period - 1, adjust=False, min_periods=period).mean()
    plus_di = 100.0 * plus_dm.ewm(com=period - 1, adjust=False, min_periods=period).mean() / atr
    minus_di = 100.0 * minus_dm.ewm(com=period - 1, adjust=False, min_periods=period).mean() / atr
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return dx.ewm(com=period - 1, adjust=False, min_periods=period).mean()


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all v1 features and return a new DataFrame with ts + feature columns.

    Warmup bars produce NaN — callers should filter or handle them explicitly.
    """
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    out = df[["ts"]].copy().reset_index(drop=True)

    # Returns
    out["returns_1"] = close.pct_change(1)
    out["returns_5"] = close.pct_change(5)
    out["log_returns_1"] = np.log(close / close.shift(1))

    # Momentum (price ratio - 1 over N bars)
    out["momentum_5"] = close / close.shift(5) - 1.0
    out["momentum_10"] = close / close.shift(10) - 1.0
    out["momentum_20"] = close / close.shift(20) - 1.0
    # 12-1 month cross-sectional momentum: 12-month return ending one month ago.
    # Skipping the most recent ~21 bars avoids short-term reversal contamination (v3).
    out["momentum_252_21"] = close.shift(21) / close.shift(252) - 1.0

    # Rolling volatility (std of log returns)
    lr = out["log_returns_1"]
    out["volatility_10"] = lr.rolling(window=10, min_periods=10).std()
    out["volatility_20"] = lr.rolling(window=20, min_periods=20).std()

    # RSI
    out["rsi_14"] = _rsi(close, 14)

    # MACD
    ema12 = _ema(close, 12)
    ema26 = _ema(close, 26)
    out["ema_12"] = ema12
    out["ema_26"] = ema26
    macd_line = ema12 - ema26
    out["macd_line"] = macd_line
    out["macd_signal"] = _ema(macd_line, 9)
    out["macd_hist"] = out["macd_line"] - out["macd_signal"]

    # Moving averages
    out["sma_20"] = _sma(close, 20)
    out["sma_50"] = _sma(close, 50)
    out["sma_200"] = _sma(close, 200)  # long-term trend / regime filter (v2)

    # Average True Range — volatility-scaled stops (v2)
    out["atr_14"] = _atr(high, low, close, 14)

    # Volume spike (z-score vs 20-bar rolling mean/std)
    vol_mean = volume.rolling(window=20, min_periods=20).mean()
    vol_std = volume.rolling(window=20, min_periods=20).std().replace(0.0, np.nan)
    out["volume_zscore_20"] = (volume - vol_mean) / vol_std

    return out


# Opening-range window in minutes from the session open (e.g. 15 ⇒ first 3 bars on 5-min).
_OPENING_RANGE_MINUTES = 15.0


def compute_intraday_features(df: pd.DataFrame) -> pd.DataFrame:
    """Session-aware intraday features (feature set `intraday_v1`).

    VWAP, opening range, cumulative-volume pace, session return, and time-of-day all **reset at
    each session boundary** (grouped by the bar's calendar date — NSE 09:15–15:30 IST never
    crosses UTC midnight, so the date key is the trading session). Continuous indicators
    (atr/rsi/ema/adx) intentionally carry across sessions: they are slow-moving and resetting
    them would blank the first ~14 bars of every day. No lookahead — every value at bar t uses
    only bars up to and including t.

    Input contract matches compute_features: ts/open/high/low/close/volume, sorted ascending.
    """
    df = df.reset_index(drop=True)
    ts = pd.to_datetime(df["ts"])
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    open_ = df["open"].astype(float)
    volume = df["volume"].astype(float)
    sess = ts.dt.date  # session key

    out = df[["ts"]].copy()

    # --- session-anchored: VWAP ---
    typical = (high + low + close) / 3.0
    cum_tpv = (typical * volume).groupby(sess).cumsum()
    cum_vol = volume.groupby(sess).cumsum().replace(0.0, np.nan)
    vwap = cum_tpv / cum_vol
    out["vwap"] = vwap
    out["vwap_dist"] = (close - vwap) / vwap
    # Scale-free stretch: distance from VWAP in per-session running-std units.
    dev = close - vwap
    sd = dev.groupby(sess).expanding().std()
    sd.index = sd.index.droplevel(0)
    sd = sd.sort_index().replace(0.0, np.nan)
    out["vwap_z"] = dev / sd

    # --- session-anchored: time-of-day + opening range ---
    session_open_ts = ts.groupby(sess).transform("min")
    out["minutes_since_open"] = (ts - session_open_ts).dt.total_seconds() / 60.0
    out["bar_in_session"] = df.groupby(sess).cumcount().astype(float)
    in_or = out["minutes_since_open"] < _OPENING_RANGE_MINUTES
    # Running OR high/low during the window; carried forward (ffill) for the rest of the session.
    or_high = high.where(in_or).groupby(sess).cummax().groupby(sess).ffill()
    or_low = low.where(in_or).groupby(sess).cummin().groupby(sess).ffill()
    out["or_high"] = or_high
    out["or_low"] = or_low
    out["or_width"] = or_high - or_low

    # --- session-anchored: volume pace + return from session open ---
    bars_so_far = out["bar_in_session"] + 1.0
    avg_vol_so_far = (cum_vol / bars_so_far).replace(0.0, np.nan)
    out["rel_volume"] = volume / avg_vol_so_far
    session_open_price = open_.groupby(sess).transform("first")
    out["session_return"] = close / session_open_price - 1.0

    # --- session-anchored: overnight gap (event-day marker) ---
    # gap_pct = today's session open vs the PRIOR session's last close, constant across the
    # session. Known at the session's first bar (both inputs are in the past — no lookahead).
    # NaN for the frame's first session (no prior close). A large |gap| marks event days
    # (earnings, news, global moves) whose intraday move distribution is fat enough to have
    # a chance of clearing round-trip cost.
    first_open = open_.groupby(sess).first()
    last_close = close.groupby(sess).last()
    gap_by_sess = first_open / last_close.shift(1) - 1.0
    out["gap_pct"] = sess.map(gap_by_sess).astype(float)

    # --- continuous (carry across sessions) ---
    out["atr_14"] = _atr(high, low, close, 14)
    out["rsi_14"] = _rsi(close, 14)
    out["ema_20"] = _ema(close, 20)
    out["adx_14"] = _adx(high, low, close, 14)

    return out
