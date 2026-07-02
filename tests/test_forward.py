"""Forward/paper-test path: prove the bus-fed engine equals a batch run over the same bars.

This equivalence is the trust anchor for "live test" — the live and historic paths share one
deterministic engine (feed/finalize), so a forward run can't silently diverge from a backtest.
"""
from datetime import date

import fakeredis

from tests.conftest import make_bar
from trading_system.agents.backtester.forward import ForwardTester
from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.results import BacktestConfig
from trading_system.bus.redis_streams import RedisBarBus
from trading_system.replay.events import BarEvent
from trading_system.strategy.momentum import MomentumStrategy


def _stream():
    """One momentum round-trip (golden cross → death cross) — same path as the determinism test."""
    return [
        make_bar(1, "AAA", close=100, open_=100, features={"sma_20": 95, "sma_50": 100, "momentum_20": 0.01}),
        make_bar(2, "AAA", close=102, open_=101, features={"sma_20": 105, "sma_50": 100, "momentum_20": 0.05}),
        make_bar(3, "AAA", close=103, open_=102, high=104, low=101, features={"sma_20": 106, "sma_50": 100, "momentum_20": 0.05}),
        make_bar(4, "AAA", close=104, open_=103, high=105, low=102, features={"sma_20": 95, "sma_50": 100, "momentum_20": -0.01}),
        make_bar(5, "AAA", close=104, open_=104, high=105, low=103, features={"sma_20": 95, "sma_50": 100, "momentum_20": -0.01}),
    ]


def _config():
    return BacktestConfig(
        strategy_name="momentum", strategy_version="v1", params={},
        symbols=["AAA"], interval="day", from_date=date(2024, 1, 1), to_date=date(2024, 1, 5),
        initial_capital=1_000_000, slippage_bps=0.0, fees_enabled=True, feature_set_version="v1",
    )


def _run_forward(stream, *, extra_publish=None):
    fr = fakeredis.FakeRedis(decode_responses=True)
    bus = RedisBarBus(client=fr, bar_stream="fwd.bars", alert_stream="fwd.alerts", maxlen=10_000)
    engine = BacktestEngine(MomentumStrategy(), _config())
    tester = ForwardTester(
        engine, bus, group="g", consumer="c",
        interval="day", symbols=["AAA"], start_id="0",  # replay everything on the stream
    )
    for b in stream:
        bus.publish(b)
    for b in extra_publish or []:
        bus.publish(b)
    while tester.step(block_ms=10) > 0:  # drain until no more accepted bars
        pass
    return tester.result(), tester


def test_forward_matches_batch_exactly():
    stream = _stream()
    batch = BacktestEngine(MomentumStrategy(), _config()).run(stream)
    fwd, _ = _run_forward(stream)

    assert [(t.entry_price, t.exit_price, t.pnl, t.fees) for t in fwd.trades] == \
           [(t.entry_price, t.exit_price, t.pnl, t.fees) for t in batch.trades]
    assert fwd.metrics == batch.metrics
    assert [(p.ts, p.equity) for p in fwd.equity_curve] == \
           [(p.ts, p.equity) for p in batch.equity_curve]


def test_forward_determinism():
    stream = _stream()
    r1, _ = _run_forward(stream)
    r2, _ = _run_forward(stream)
    assert [(t.entry_price, t.exit_price, t.pnl) for t in r1.trades] == \
           [(t.entry_price, t.exit_price, t.pnl) for t in r2.trades]
    assert r1.metrics == r2.metrics


def test_forward_filters_other_intervals_and_symbols():
    stream = _stream()
    noise = [
        make_bar(1, "ZZZ", close=50, features={}),  # wrong symbol
        BarEvent(ts=stream[0].ts, symbol="AAA", interval="minute", instrument_token=1,
                 open=1, high=1, low=1, close=1, volume=1, features={}),  # wrong interval
    ]
    _, tester = _run_forward(stream, extra_publish=noise)
    assert tester.fed == len(stream)  # only the day/AAA bars were fed to the engine
