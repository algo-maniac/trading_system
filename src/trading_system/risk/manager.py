"""RiskManager: the single gate every order passes through.

- SELL (risk-reducing) is always approved for the full open position.
- BUY is sized from the signal's size_hint, then capped by every portfolio limit.
  If new entries are halted (daily loss / drawdown), BUYs are rejected.

evaluate() is pure given its inputs (no I/O) so it's fully unit-testable.
"""
from trading_system.portfolio.portfolio import Portfolio
from trading_system.risk import rules
from trading_system.risk.models import RiskAction, RiskConfig, RiskDecision
from trading_system.strategy.signals import Side, Signal


class RiskManager:
    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()

    def evaluate(
        self,
        signal: Signal,
        portfolio: Portfolio,
        ref_price: float,
        day_return_pct: float = 0.0,
        current_drawdown: float = 0.0,
    ) -> RiskDecision:
        # Exits always allowed — reducing risk never blocked.
        if signal.side == Side.SELL:
            qty = portfolio.position_qty(signal.symbol)
            if qty <= 0:
                return RiskDecision(RiskAction.REJECT, 0, "no_position_to_sell")
            return RiskDecision(RiskAction.APPROVE, qty, "exit_approved")

        if signal.side != Side.BUY:
            return RiskDecision(RiskAction.REJECT, 0, "non_actionable_signal")

        # --- BUY path ---
        halt = rules.entries_halted(day_return_pct, current_drawdown, self.config)
        if halt:
            return RiskDecision(RiskAction.REJECT, 0, halt)

        is_new_symbol = not portfolio.has_position(signal.symbol)
        if not rules.can_open_new_position(len(portfolio.positions), self.config.max_positions, is_new_symbol):
            return RiskDecision(RiskAction.REJECT, 0, f"max_positions ({self.config.max_positions}) reached")

        equity = portfolio.equity()
        desired_qty = int((signal.size_hint * equity) // ref_price) if ref_price > 0 else 0
        if desired_qty <= 0:
            return RiskDecision(RiskAction.REJECT, 0, "size_hint_zero_or_price_invalid")

        caps = {
            "position_pct": rules.max_qty_for_position_pct(equity, ref_price, self.config.max_position_pct),
            "gross_exposure": rules.max_qty_for_exposure(
                equity, portfolio.gross_exposure(), ref_price, self.config.max_gross_exposure_pct
            ),
            "cash": rules.max_qty_for_cash(portfolio.cash, ref_price),
        }
        binding_cap = min(caps.values())
        approved = min(desired_qty, binding_cap)

        if approved <= 0:
            limiter = min(caps, key=lambda k: caps[k])
            return RiskDecision(RiskAction.REJECT, 0, f"capped_to_zero_by_{limiter}")

        if approved < desired_qty:
            limiter = min(caps, key=lambda k: caps[k])
            return RiskDecision(RiskAction.RESIZE, approved, f"resized_by_{limiter} {approved}/{desired_qty}")

        return RiskDecision(RiskAction.APPROVE, approved, "approved")
