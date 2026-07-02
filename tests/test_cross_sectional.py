"""Cross-sectional momentum: strategy ranking/rebalance logic + engine batched path."""
from datetime import date

from tests.conftest import make_bar
from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.results import BacktestConfig
from trading_system.strategy.cross_sectional import CrossSectionalMomentum
from trading_system.strategy.signals import Side


class FakePortfolio:
    """Minimal cross-sectional PortfolioView stub."""
    def __init__(self, held=None):
        self._held = set(held or [])

    def has_position(self, symbol): return symbol in self._held
    def position_qty(self, symbol): return 100 if symbol in self._held else 0
    def entry_price(self, symbol): return 100.0
    def bars_held(self, symbol): return 0
    def open_symbols(self): return list(self._held)


def _batch(day, momentum: dict):
    """One timestamp's worth of bars, each carrying a momentum_252_21 value."""
    return [
        make_bar(day, symbol=sym, close=100.0, features={"momentum_252_21": m})
        for sym, m in momentum.items()
    ]


# --- Strategy unit tests ---

def test_first_rebalance_queues_buys_emitted_next_bar():
    s = CrossSectionalMomentum(top_n=2, rebalance_bars=5)
    pf = FakePortfolio()
    # bar_count 0 → rebalance. Nothing held → no sells; buys queued, not emitted yet.
    assert s.on_bars(_batch(1, {"A": 0.3, "B": 0.1, "C": 0.5}), pf) == []
    # next bar → drain queued buys (top-2 = C, A by momentum)
    sigs = s.on_bars(_batch(2, {"A": 0.3, "B": 0.1, "C": 0.5}), pf)
    assert {x.symbol for x in sigs} == {"C", "A"}
    assert all(x.side == Side.BUY for x in sigs)
    assert all(x.size_hint == 0.5 for x in sigs)  # equal weight 1/top_n


def test_rotation_sells_drop_then_buys_replacement_next_bar():
    s = CrossSectionalMomentum(top_n=2, rebalance_bars=3)
    pf = FakePortfolio(held=["A", "C"])
    # rebalance: top-2 = C, B → A drops out, B is the new entry
    sigs = s.on_bars(_batch(1, {"A": 0.1, "B": 0.4, "C": 0.5}), pf)
    assert {x.symbol for x in sigs if x.side == Side.SELL} == {"A"}
    assert not any(x.side == Side.BUY for x in sigs)   # buy deferred until cash settles
    pf._held = {"C"}                                    # simulate A's sell filling
    # next bar (not a rebalance) → drain the queued buy for B
    sigs2 = s.on_bars(_batch(2, {"A": 0.1, "B": 0.4, "C": 0.5}), pf)
    assert {x.symbol for x in sigs2 if x.side == Side.BUY} == {"B"}


def test_no_signals_between_rebalances():
    s = CrossSectionalMomentum(top_n=2, rebalance_bars=5)
    pf = FakePortfolio()
    s.on_bars(_batch(1, {"A": 0.3, "B": 0.5}), pf)   # rebalance, queue buys
    s.on_bars(_batch(2, {"A": 0.3, "B": 0.5}), pf)   # drain buys
    pf._held = {"A", "B"}
    # bars 3,4,5 are not rebalance bars and nothing is queued → no signals
    for d in (3, 4, 5):
        assert s.on_bars(_batch(d, {"A": 0.3, "B": 0.5}), pf) == []


def test_missing_momentum_excluded_from_ranking():
    s = CrossSectionalMomentum(top_n=2, rebalance_bars=5)
    pf = FakePortfolio()
    bars = [
        make_bar(1, symbol="A", features={"momentum_252_21": 0.3}),
        make_bar(1, symbol="B", features={"momentum_252_21": 0.1}),
        make_bar(1, symbol="C", features={}),  # warmup NaN → excluded even with no rivals
    ]
    s.on_bars(bars, pf)  # rebalance: top-2 of the two valid names = A, B
    sigs = s.on_bars(
        [make_bar(2, symbol=x, features={"momentum_252_21": 0.0}) for x in ("A", "B", "C")], pf
    )
    buys = {x.symbol for x in sigs if x.side == Side.BUY}
    assert buys == {"A", "B"} and "C" not in buys


# --- Engine batched-path integration ---

def _xsec_config():
    return BacktestConfig(
        strategy_name="xsec_momentum", strategy_version="v1", params={},
        symbols=["AAA", "BBB", "CCC"], interval="day",
        from_date=date(2024, 1, 1), to_date=date(2024, 1, 10),
        initial_capital=1_000_000, slippage_bps=0.0, fees_enabled=False,
        feature_set_version="v3",
    )


def _rotation_stream():
    """3 symbols over 8 days; CCC overtakes BBB after day 4, forcing one rotation."""
    bars = []
    for d in range(1, 9):
        # CCC overtakes BBB after day 4
        mom = {"AAA": 0.5, "BBB": 0.3, "CCC": 0.1}
        if d > 4:
            mom = {"AAA": 0.5, "BBB": 0.1, "CCC": 0.4}
        for sym, m in mom.items():
            bars.append(make_bar(
                d, symbol=sym, close=100.0, open_=100.0, features={"momentum_252_21": m},
            ))
    return bars


def test_engine_cross_sectional_round_trip_and_rotation():
    engine = BacktestEngine(CrossSectionalMomentum(top_n=2, rebalance_bars=2), _xsec_config())
    result = engine.run(_rotation_stream())

    assert result.equity_curve  # batched path snapshots equity per timestamp
    # Ends holding the final top-2 (AAA always top; CCC overtook BBB)
    assert set(engine.portfolio.open_symbols()) == {"AAA", "CCC"}
    # The rotation closed BBB as a completed round-trip
    assert any(t.symbol == "BBB" for t in result.trades)


def test_engine_cross_sectional_determinism():
    def run():
        return BacktestEngine(
            CrossSectionalMomentum(top_n=2, rebalance_bars=2), _xsec_config()
        ).run(_rotation_stream())

    r1, r2 = run(), run()
    assert [(t.symbol, t.entry_price, t.exit_price, t.pnl) for t in r1.trades] == \
           [(t.symbol, t.entry_price, t.exit_price, t.pnl) for t in r2.trades]
    assert r1.metrics == r2.metrics
    eq1 = [(p.ts, p.equity) for p in r1.equity_curve]
    eq2 = [(p.ts, p.equity) for p in r2.equity_curve]
    assert eq1 == eq2


# --- Breadth regime filter ---

def _batch_with_breadth(day, momentum: dict, above: set):
    """Bars carrying momentum + sma_200; symbols in `above` trade over their SMA."""
    return [
        make_bar(
            day, symbol=sym, close=100.0,
            features={"momentum_252_21": m, "sma_200": 90.0 if sym in above else 110.0},
        )
        for sym, m in momentum.items()
    ]


def test_breadth_filter_disabled_by_default():
    s = CrossSectionalMomentum(top_n=2, rebalance_bars=5)
    pf = FakePortfolio()
    s.on_bars(_batch_with_breadth(1, {"A": 0.3, "B": 0.1, "C": 0.5}, above=set()), pf)
    sigs = s.on_bars(_batch_with_breadth(2, {"A": 0.3, "B": 0.1, "C": 0.5}, above=set()), pf)
    assert {x.symbol for x in sigs} == {"C", "A"}  # zero breadth ignored when disabled


def test_breadth_below_min_sells_all_and_queues_nothing():
    s = CrossSectionalMomentum(top_n=2, rebalance_bars=3, breadth_min=0.5)
    pf = FakePortfolio(held=["A", "C"])
    # 1 of 3 above SMA → breadth 0.33 < 0.5 → risk-off: sell everything, queue nothing.
    sigs = s.on_bars(_batch_with_breadth(1, {"A": 0.1, "B": 0.4, "C": 0.5}, above={"A"}), pf)
    assert {x.symbol for x in sigs if x.side == Side.SELL} == {"A", "C"}
    assert not any(x.side == Side.BUY for x in sigs)
    pf._held = set()
    # next bar: no queued buys drain
    assert s.on_bars(_batch_with_breadth(2, {"A": 0.1, "B": 0.4, "C": 0.5}, above={"A"}), pf) == []


def test_breadth_recovery_reenters_at_next_rebalance():
    s = CrossSectionalMomentum(top_n=1, rebalance_bars=2, breadth_min=0.5)
    pf = FakePortfolio()
    # bar 0: rebalance, breadth 0 → risk-off
    assert s.on_bars(_batch_with_breadth(1, {"A": 0.3, "B": 0.5}, above=set()), pf) == []
    # bar 1: not a rebalance
    assert s.on_bars(_batch_with_breadth(2, {"A": 0.3, "B": 0.5}, above=set()), pf) == []
    # bar 2: rebalance, breadth 1.0 → target top-1 (B) queued
    assert s.on_bars(_batch_with_breadth(3, {"A": 0.3, "B": 0.5}, above={"A", "B"}), pf) == []
    sigs = s.on_bars(_batch_with_breadth(4, {"A": 0.3, "B": 0.5}, above={"A", "B"}), pf)
    assert {x.symbol for x in sigs} == {"B"}


def test_breadth_warmup_without_sma_is_neutral():
    s = CrossSectionalMomentum(top_n=1, rebalance_bars=5, breadth_min=0.5)
    pf = FakePortfolio()
    # no sma_200 anywhere → breadth treated as 1.0 → filter passes
    s.on_bars(_batch(1, {"A": 0.3, "B": 0.5}), pf)
    sigs = s.on_bars(_batch(2, {"A": 0.3, "B": 0.5}), pf)
    assert {x.symbol for x in sigs} == {"B"}


# --- Crash quarantine (demerger guard) ---

def _crash_batch(day, closes: dict, momentum=0.5):
    return [
        make_bar(day, symbol=sym, close=c, features={"momentum_252_21": momentum})
        for sym, c in closes.items()
    ]


def test_crash_quarantine_excludes_from_ranking_and_force_sells():
    s = CrossSectionalMomentum(top_n=1, rebalance_bars=1, crash_quarantine_bars=5)
    pf = FakePortfolio(held=["A"])
    # day 1: establish prev closes (A ranks top-1 and is held → no signals)
    assert s.on_bars(_crash_batch(1, {"A": 100.0, "B": 100.0}), pf) == []
    # day 2: A drops 40% in one bar (demerger-style) → force SELL + excluded from target
    sigs = s.on_bars(_crash_batch(2, {"A": 60.0, "B": 100.0}), pf)
    sells = [x for x in sigs if x.side == Side.SELL and x.symbol == "A"]
    assert sells and "quarantine" in sells[0].reason
    pf._held = set()
    # day 3: rebalance target must be B (A quarantined despite equal momentum feature)
    s.on_bars(_crash_batch(3, {"A": 60.0, "B": 100.0}), pf)
    sigs4 = s.on_bars(_crash_batch(4, {"A": 60.0, "B": 100.0}), pf)
    assert {x.symbol for x in sigs4 if x.side == Side.BUY} == {"B"}


def test_crash_quarantine_readmits_after_window():
    s = CrossSectionalMomentum(top_n=1, rebalance_bars=1, crash_quarantine_bars=3)
    pf = FakePortfolio()
    # A has higher momentum than B, crashes on day 2, quarantined for 3 bars
    s.on_bars(_crash_batch(1, {"A": 100.0, "B": 100.0}), pf)
    s.on_bars([make_bar(2, symbol="A", close=60.0, features={"momentum_252_21": 0.9}),
               make_bar(2, symbol="B", close=100.0, features={"momentum_252_21": 0.5})], pf)
    # bars 3..4: still quarantined → B is the target
    for d in (3, 4):
        s.on_bars([make_bar(d, symbol="A", close=60.0, features={"momentum_252_21": 0.9}),
                   make_bar(d, symbol="B", close=100.0, features={"momentum_252_21": 0.5})], pf)
    # bar 5 (bar_count now past quarantine window): A re-admitted, outranks B
    sigs = s.on_bars([make_bar(5, symbol="A", close=60.0, features={"momentum_252_21": 0.9}),
                      make_bar(6, symbol="B", close=100.0, features={"momentum_252_21": 0.5})], pf)
    drained = s.on_bars([make_bar(7, symbol="A", close=60.0, features={"momentum_252_21": 0.9}),
                         make_bar(7, symbol="B", close=100.0, features={"momentum_252_21": 0.5})], pf)
    bought = {x.symbol for x in (sigs + drained) if x.side == Side.BUY}
    assert "A" in bought


def test_crash_quarantine_disabled_with_zero_bars():
    s = CrossSectionalMomentum(top_n=1, rebalance_bars=1, crash_quarantine_bars=0)
    pf = FakePortfolio()
    s.on_bars(_crash_batch(1, {"A": 100.0}), pf)
    s.on_bars(_crash_batch(2, {"A": 55.0}), pf)  # 45% crash, guard off
    sigs = s.on_bars(_crash_batch(3, {"A": 55.0}), pf)
    assert {x.symbol for x in sigs if x.side == Side.BUY} == {"A"}
