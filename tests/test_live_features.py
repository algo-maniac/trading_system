"""Live feature frame: gap_pct from prior session, warmup carryover, NaN dropping."""
from datetime import datetime

import pytest

from trading_system.agents.reader.live_features import latest_features
from trading_system.replay.events import BarEvent


def _bar(day, hh, mm, close, volume=1000):
    return BarEvent(ts=datetime(2026, 7, day, hh, mm), symbol="X", interval="5minute",
                    instrument_token=1, open=close, high=close + 0.5, low=close - 0.5,
                    close=close, volume=volume, features={})


def _session(day, base):
    """A full 75-bar 5-min session with slight drift."""
    bars = []
    for i in range(75):
        hh, mm = divmod(9 * 60 + 15 + 5 * i, 60)
        bars.append(_bar(day, hh, mm, base + i * 0.1))
    return bars


def test_gap_pct_uses_prior_session_close():
    prior = _session(2, 100.0)                      # closes at 100 + 74*0.1 = 107.4
    today = [_bar(3, 9, 15, 110.0), _bar(3, 9, 20, 111.0)]
    feats = latest_features(prior + today)
    assert "gap_pct" in feats
    assert feats["gap_pct"] == pytest.approx(110.0 / 107.4 - 1.0)


def test_continuous_indicators_present_early_in_session_via_history():
    prior = _session(1, 100.0) + _session(2, 105.0)
    today = [_bar(3, 9, 15, 113.0)]
    feats = latest_features(prior + today)
    # atr/rsi warmed up on prior sessions → available on today's FIRST bar
    assert "atr_14" in feats and "rsi_14" in feats
    # without history, the same first bar has no warmed indicators
    feats_bare = latest_features(today)
    assert "atr_14" not in feats_bare


def test_empty_input_returns_empty():
    assert latest_features([]) == {}
