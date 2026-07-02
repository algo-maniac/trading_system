"""Trust anchor: the paper pipeline (Strategist → Trader → PaperBroker → ledger) produces
the SAME economics as the backtest engine over the same bars.

Uses xsec_momentum (rule exits only — no engine-side stops, whose fill timing legitimately
differs between engine and paper) with identical risk config, zero slippage, fees off.
Cash after the run must match to the rupee; trades must match in count and P&L.
"""
from datetime import datetime, timezone

import pytest

from tests.fakes import FakeLedger
from trading_system.agents.strategist.runner import Sleeve as StratSleeve
from trading_system.agents.strategist.runner import StrategistRunner
from trading_system.agents.trader.core import SleeveExecution, TraderCore
from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.results import BacktestConfig
from trading_system.execution.fees import Product
from trading_system.execution.paper_broker import PaperBroker
from trading_system.portfolio.live import LivePortfolio
from trading_system.replay.events import BarEvent
from trading_system.risk.models import RiskConfig
from trading_system.strategy.cross_sectional import CrossSectionalMomentum

_UTC = timezone.utc


def _bars() -> list[BarEvent]:
    """3 symbols × 8 days; momentum ranks rotate so the top-2 book turns over."""
    out = []
    mom = {
        "AAA": [0.9, 0.9, 0.9, 0.2, 0.2, 0.2, 0.2, 0.2],
        "BBB": [0.5, 0.5, 0.5, 0.8, 0.8, 0.8, 0.8, 0.8],
        "CCC": [0.1, 0.1, 0.1, 0.6, 0.6, 0.6, 0.6, 0.6],
    }
    px = {
        "AAA": [100, 102, 104, 103, 101, 100, 99, 98],
        "BBB": [50, 51, 52, 53, 55, 56, 57, 58],
        "CCC": [200, 201, 199, 202, 205, 207, 210, 212],
    }
    for d in range(8):
        for sym in ("AAA", "BBB", "CCC"):
            p = float(px[sym][d])
            out.append(BarEvent(
                ts=datetime(2026, 6, 1 + d, tzinfo=_UTC), symbol=sym, interval="day",
                instrument_token=hash(sym) % 1000, open=p, high=p, low=p, close=p,
                volume=10_000, features={"momentum_252_21": mom[sym][d]},
            ))
    return out


_RISK = dict(max_position_pct=1.0, max_positions=25, max_gross_exposure_pct=1.0)
_PARAMS = dict(top_n=2, rebalance_bars=3)
CAPITAL = 1_000_000.0


def _engine_run(bars):
    strat = CrossSectionalMomentum(**_PARAMS)
    cfg = BacktestConfig(
        strategy_name=strat.name, strategy_version=strat.version, params=strat.params,
        symbols=["AAA", "BBB", "CCC"], interval="day",
        from_date=bars[0].ts.date(), to_date=bars[-1].ts.date(),
        initial_capital=CAPITAL, slippage_bps=0.0, fees_enabled=False,
        feature_set_version="v3",
    )
    engine = BacktestEngine(strat, cfg, risk_config=RiskConfig(**_RISK))
    result = engine.run(iter(bars))
    return engine.portfolio, result


def _pipeline_run(bars):
    ledger = FakeLedger()
    broker = PaperBroker(ledger, slippage_bps=0.0, fees_enabled=False)
    core = TraderCore(
        ledger, broker,
        sleeves=[SleeveExecution("momentum", Product.CNC, RiskConfig(**_RISK))],
        paper_capital=CAPITAL, sleeve_weights={"momentum": 1.0},
    )
    view = LivePortfolio(ledger, "momentum")
    runner = StrategistRunner(
        [StratSleeve("momentum", CrossSectionalMomentum(**_PARAMS), "day", view)],
        publish=core.on_signal,  # direct wiring — bus serde identity is tested elsewhere
    )
    for bar in bars:
        # Trader marks prices/fills first (bar open), then the strategist reacts to the
        # bar — same intra-timestamp ordering as the engine's process-fills-then-signal.
        core.on_bar(bar)
        runner.on_bar(bar)
    runner.flush()
    return ledger, view


def test_paper_pipeline_matches_engine_economics():
    bars = _bars()
    engine_pf, engine_result = _engine_run(bars)
    ledger, view = _pipeline_run(bars)

    assert view.cash == pytest.approx(engine_pf.cash)
    engine_open = {s: p.qty for s, p in engine_pf.positions.items()}
    paper_open = {p.symbol: p.qty for p in ledger.open_positions("momentum")}
    assert paper_open == engine_open

    engine_realized = sum(t.pnl for t in engine_result.trades)
    paper_realized = sum(p["realized_pnl"] for p in ledger.positions if p["closed_ts"])
    assert paper_realized == pytest.approx(engine_realized)
    assert len([p for p in ledger.positions if p["closed_ts"]]) == len(engine_result.trades)
