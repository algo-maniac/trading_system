"""Risk models: configuration and decision types."""
from dataclasses import dataclass
from enum import Enum


class RiskAction(str, Enum):
    APPROVE = "APPROVE"   # accept the order as sized
    RESIZE = "RESIZE"     # accept but with a reduced quantity
    REJECT = "REJECT"     # block the order entirely


@dataclass(frozen=True)
class RiskConfig:
    """Portfolio-level, long-only risk limits."""
    max_position_pct: float = 0.10      # single position ≤ 10% of equity
    max_positions: int = 10             # max concurrent open positions
    max_gross_exposure_pct: float = 1.0 # total long exposure ≤ 100% of equity (no leverage)
    daily_loss_limit_pct: float = 0.05  # halt new entries if day's loss exceeds 5%
    max_drawdown_pct: float = 0.20      # halt new entries if drawdown from peak exceeds 20%
    # The drawdown halt references the equity peak over this many most-recent TRADING DAYS,
    # not the all-time peak. Without a window, one deep crash halts entries forever: equity
    # goes flat (nothing held, nothing bought), the drawdown vs. the old peak never shrinks,
    # and the system is dead — the 2016-2022 Nifty-200 run sat in cash from COVID to the end
    # (zero trades in 2021-22). With a rolling window the reference peak decays after a
    # crash-plus-recovery period and entries re-arm; a fresh crash still halts immediately.
    # 0 = legacy all-time-peak behavior. Reporting max-drawdown metrics stay all-time.
    halt_peak_window_days: int = 252


@dataclass(frozen=True)
class RiskDecision:
    action: RiskAction
    approved_qty: int
    reason: str
