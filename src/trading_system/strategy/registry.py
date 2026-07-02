"""Strategy registry — maps strategy name to class for CLI/engine lookup.

Every strategy is versioned (Strategy.version) and measurable, per the platform rules.
"""
from trading_system.strategy.base import Strategy
from trading_system.strategy.cross_sectional import CrossSectionalMomentum
from trading_system.strategy.mean_reversion import MeanReversionStrategy, MeanReversionV2
from trading_system.strategy.momentum import MomentumStrategy, MomentumV2, MomentumV3
from trading_system.strategy.orb import OpeningRangeBreakout
from trading_system.strategy.swing_breakout import SwingBreakout
from trading_system.strategy.swing_pullback import SwingPullback
from trading_system.strategy.vwap_reversion import VWAPReversion

STRATEGIES: dict[str, type[Strategy]] = {
    MomentumStrategy.name: MomentumStrategy,
    MomentumV2.name: MomentumV2,
    MomentumV3.name: MomentumV3,
    MeanReversionStrategy.name: MeanReversionStrategy,
    MeanReversionV2.name: MeanReversionV2,
    CrossSectionalMomentum.name: CrossSectionalMomentum,
    # Phase 4b — intraday (MIS, EOD square-off) + swing (CNC, multi-day hold)
    VWAPReversion.name: VWAPReversion,
    OpeningRangeBreakout.name: OpeningRangeBreakout,
    SwingPullback.name: SwingPullback,
    SwingBreakout.name: SwingBreakout,
}


def get_strategy(name: str, **params: float) -> Strategy:
    if name not in STRATEGIES:
        raise KeyError(f"Unknown strategy '{name}'. Available: {sorted(STRATEGIES)}")
    return STRATEGIES[name](**params)


def list_strategies() -> list[str]:
    return sorted(STRATEGIES)
