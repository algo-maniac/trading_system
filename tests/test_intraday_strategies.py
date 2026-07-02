"""Intraday strategy logic: VWAP-reversion + Opening-Range Breakout entry/exit gating."""
from tests.conftest import make_bar
from trading_system.strategy.orb import OpeningRangeBreakout
from trading_system.strategy.signals import Side
from trading_system.strategy.vwap_reversion import VWAPReversion


class FakePortfolio:
    def __init__(self, holding=False, bars=0):
        self._holding, self._bars = holding, bars

    def has_position(self, symbol): return self._holding
    def position_qty(self, symbol): return 100 if self._holding else 0
    def entry_price(self, symbol): return 100.0
    def bars_held(self, symbol): return self._bars
    def open_symbols(self): return ["TEST"] if self._holding else []


# --- VWAP reversion ---

def _vwap_feats(**over):
    # close=100 vs vwap=102 → vwap_dist ≈ -1.96%, well past the 0.4% min_edge gate.
    f = {"vwap": 102.0, "vwap_dist": -0.0196, "vwap_z": -2.0, "adx_14": 18.0,
         "rsi_14": 28.0, "bar_in_session": 10.0, "atr_14": 1.0}
    f.update(over)
    return f


def test_vwap_enters_when_stretched_range_oversold_post_or():
    s = VWAPReversion(z_entry=1.5, adx_max=25, rsi_max=35, or_bars=3)
    sigs = s.on_bar(make_bar(1, close=100.0, features=_vwap_feats()), FakePortfolio())
    assert len(sigs) == 1 and sigs[0].side == Side.BUY
    # Fixed (non-trailing) k×ATR stop: 2.0 × atr(1.0) / close(100) = 0.02.
    assert abs(sigs[0].stop_pct - 0.02) < 1e-9
    assert sigs[0].trailing_atr_mult is None  # fade trade uses a fixed stop, never trailing


def test_vwap_no_entry_when_trending_or_not_stretched_or_pre_or():
    s = VWAPReversion()
    pf = FakePortfolio()
    assert s.on_bar(make_bar(1, close=100, features=_vwap_feats(adx_14=40)), pf) == []   # trend day
    assert s.on_bar(make_bar(1, close=100, features=_vwap_feats(vwap_z=-0.5)), pf) == []  # not stretched (z)
    assert s.on_bar(make_bar(1, close=100, features=_vwap_feats(vwap_dist=-0.001)), pf) == []  # stretch too small to clear cost
    assert s.on_bar(make_bar(1, close=100, features=_vwap_feats(rsi_14=60)), pf) == []    # not oversold
    assert s.on_bar(make_bar(1, close=100, features=_vwap_feats(bar_in_session=1)), pf) == []  # pre-OR


def test_vwap_exits_on_revert_to_vwap():
    s = VWAPReversion()
    # holding, close back at/above VWAP → SELL (target reached)
    sigs = s.on_bar(make_bar(2, close=103.0, features=_vwap_feats(vwap=102.0)), FakePortfolio(holding=True))
    assert len(sigs) == 1 and sigs[0].side == Side.SELL and sigs[0].reason == "vwap_revert"


def test_vwap_exits_on_time_stop():
    s = VWAPReversion(time_stop_bars=12)
    # holding, still below VWAP but held too long → time-stop SELL
    sigs = s.on_bar(make_bar(2, close=100.0, features=_vwap_feats(vwap=102.0)), FakePortfolio(holding=True, bars=12))
    assert len(sigs) == 1 and sigs[0].side == Side.SELL and sigs[0].reason == "time_stop"


# --- Opening-Range Breakout ---

def _orb_feats(**over):
    f = {"or_high": 100.0, "or_low": 98.0, "or_width": 2.0, "rel_volume": 2.0,
         "adx_14": 30.0, "bar_in_session": 10.0, "atr_14": 1.0}
    f.update(over)
    return f


def test_orb_enters_on_confirmed_breakout_trend_day():
    s = OpeningRangeBreakout(vol_mult=1.5, adx_min=20, or_bars=3, target_r=2.0)
    sigs = s.on_bar(make_bar(1, close=101.0, features=_orb_feats()), FakePortfolio())  # close>or_high
    assert len(sigs) == 1 and sigs[0].side == Side.BUY
    assert sigs[0].stop_pct is not None and sigs[0].target_pct is not None
    # target distance is target_r × OR width
    assert abs(sigs[0].target_pct - (2.0 * 2.0) / 101.0) < 1e-9


def test_orb_no_entry_without_break_volume_or_trend():
    s = OpeningRangeBreakout(vol_mult=1.5, adx_min=20, or_bars=3)
    pf = FakePortfolio()
    assert s.on_bar(make_bar(1, close=99.0, features=_orb_feats()), pf) == []                 # below OR high
    assert s.on_bar(make_bar(1, close=101.0, features=_orb_feats(rel_volume=1.0)), pf) == []  # weak volume
    assert s.on_bar(make_bar(1, close=101.0, features=_orb_feats(adx_14=10)), pf) == []        # range day
    assert s.on_bar(make_bar(1, close=101.0, features=_orb_feats(bar_in_session=1)), pf) == [] # pre-OR


def test_orb_holds_without_strategy_exit():
    s = OpeningRangeBreakout()
    # exits are engine-side (stop/target/EOD), so a held position emits nothing
    assert s.on_bar(make_bar(2, close=105.0, features=_orb_feats()), FakePortfolio(holding=True)) == []


# --- ORB gap-day event filter (intraday_v2's gap_pct) ---

def test_orb_gap_filter_disabled_by_default():
    s = OpeningRangeBreakout()
    # no gap_pct in features at all → still enters when the filter is off
    sigs = s.on_bar(make_bar(1, close=101.0, features=_orb_feats()), FakePortfolio())
    assert len(sigs) == 1


def test_orb_gap_filter_requires_gap_up():
    s = OpeningRangeBreakout(gap_min=0.01)
    pf = FakePortfolio()
    # gap too small / gap down / missing → no entry
    assert s.on_bar(make_bar(1, close=101.0, features=_orb_feats(gap_pct=0.004)), pf) == []
    assert s.on_bar(make_bar(1, close=101.0, features=_orb_feats(gap_pct=-0.02)), pf) == []
    assert s.on_bar(make_bar(1, close=101.0, features=_orb_feats()), pf) == []
    # big enough gap-up → entry
    sigs = s.on_bar(make_bar(1, close=101.0, features=_orb_feats(gap_pct=0.015)), pf)
    assert len(sigs) == 1 and sigs[0].side == Side.BUY
