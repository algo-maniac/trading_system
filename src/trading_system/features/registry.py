"""Feature set registry: versioned definitions of what features exist and how.

Adding a new feature set: create a new entry in FEATURE_SETS with a new version string.
Old versions remain in the DB for reproducibility; new ingestion writes to the new version.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class FeatureSetConfig:
    version: str
    description: str
    feature_names: list[str]
    warmup_bars: int  # minimum bars before any non-NaN output
    intraday: bool = False  # if True, build via session-aware compute_intraday_features


FEATURE_SETS: dict[str, FeatureSetConfig] = {
    "v1": FeatureSetConfig(
        version="v1",
        description="Base technical indicators: returns, momentum, volatility, RSI, MACD, MAs, volume spike",
        feature_names=[
            "returns_1",
            "returns_5",
            "log_returns_1",
            "momentum_5",
            "momentum_10",
            "momentum_20",
            "volatility_10",
            "volatility_20",
            "rsi_14",
            "macd_line",
            "macd_signal",
            "macd_hist",
            "ema_12",
            "ema_26",
            "sma_20",
            "sma_50",
            "volume_zscore_20",
        ],
        warmup_bars=52,  # EMA-26 (26 bars) + MACD signal EMA-9 = ~35; SMA-50 needs 50
    ),
    "v2": FeatureSetConfig(
        version="v2",
        description="v1 indicators + sma_200 (regime filter) and atr_14 (volatility stops)",
        feature_names=[
            "returns_1",
            "returns_5",
            "log_returns_1",
            "momentum_5",
            "momentum_10",
            "momentum_20",
            "volatility_10",
            "volatility_20",
            "rsi_14",
            "macd_line",
            "macd_signal",
            "macd_hist",
            "ema_12",
            "ema_26",
            "sma_20",
            "sma_50",
            "sma_200",
            "atr_14",
            "volume_zscore_20",
        ],
        warmup_bars=200,  # SMA-200 needs 200 bars before any non-NaN value
    ),
    "v3": FeatureSetConfig(
        version="v3",
        description="v2 indicators + momentum_252_21 (12-1 month cross-sectional momentum)",
        feature_names=[
            "returns_1",
            "returns_5",
            "log_returns_1",
            "momentum_5",
            "momentum_10",
            "momentum_20",
            "momentum_252_21",
            "volatility_10",
            "volatility_20",
            "rsi_14",
            "macd_line",
            "macd_signal",
            "macd_hist",
            "ema_12",
            "ema_26",
            "sma_20",
            "sma_50",
            "sma_200",
            "atr_14",
            "volume_zscore_20",
        ],
        warmup_bars=252,  # momentum_252_21 needs 252 bars of history
    ),
    "intraday_v1": FeatureSetConfig(
        version="intraday_v1",
        description=(
            "Session-aware intraday features: VWAP (+dist/z), opening range, rel-volume, "
            "time-of-day, session return + continuous atr/rsi/ema_20/adx (regime)"
        ),
        feature_names=[
            "vwap",
            "vwap_dist",
            "vwap_z",
            "or_high",
            "or_low",
            "or_width",
            "rel_volume",
            "minutes_since_open",
            "bar_in_session",
            "session_return",
            "atr_14",
            "rsi_14",
            "ema_20",
            "adx_14",
        ],
        warmup_bars=30,  # continuous atr/rsi/adx settle in ~14-28 bars; session features from bar 1
        intraday=True,
    ),
    "intraday_v2": FeatureSetConfig(
        version="intraday_v2",
        description=(
            "intraday_v1 + gap_pct (overnight gap vs prior session close — event-day marker "
            "for gap/event-filtered intraday strategies)"
        ),
        feature_names=[
            "vwap",
            "vwap_dist",
            "vwap_z",
            "or_high",
            "or_low",
            "or_width",
            "rel_volume",
            "minutes_since_open",
            "bar_in_session",
            "session_return",
            "gap_pct",
            "atr_14",
            "rsi_14",
            "ema_20",
            "adx_14",
        ],
        warmup_bars=30,
        intraday=True,
    ),
}

DEFAULT_FEATURE_SET = "v1"
