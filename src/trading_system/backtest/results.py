"""Backtest result container + reproducibility hashing."""
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date

from trading_system.portfolio.models import Trade
from trading_system.portfolio.portfolio import EquityPoint


@dataclass
class BacktestConfig:
    strategy_name: str
    strategy_version: str
    params: dict          # type: ignore[type-arg]
    symbols: list[str]
    interval: str
    from_date: date
    to_date: date
    initial_capital: float
    slippage_bps: float
    fees_enabled: bool
    feature_set_version: str
    product: str = "CNC"  # "CNC" (delivery/overnight) or "MIS" (intraday) — drives the fee model

    def config_hash(self) -> str:
        """Stable hash of everything that affects the result — for reproducibility."""
        payload = json.dumps({
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "params": self.params,
            "symbols": sorted(self.symbols),
            "interval": self.interval,
            "from_date": self.from_date.isoformat(),
            "to_date": self.to_date.isoformat(),
            "initial_capital": self.initial_capital,
            "slippage_bps": self.slippage_bps,
            "fees_enabled": self.fees_enabled,
            "feature_set_version": self.feature_set_version,
            "product": self.product,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class SignalAudit:
    ts: object            # datetime
    symbol: str
    side: str
    risk_action: str
    approved_qty: int
    reason: str


@dataclass
class BacktestResult:
    config: BacktestConfig
    equity_curve: list[EquityPoint]
    trades: list[Trade]
    signals: list[SignalAudit]
    metrics: dict = field(default_factory=dict)  # type: ignore[type-arg]
