"""Capital-expenditure/reinvestment planning; NEVER moves money or changes risk limits."""
from __future__ import annotations
from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class ReinvestmentPlan:
    after_cost_realized_profit: float
    tax_reserve: float
    available_liquid_cash: float
    emergency_cash_floor: float
    reinvestment_rate: float = 0.20

    def __post_init__(self):
        if any(x < 0 for x in (self.tax_reserve, self.available_liquid_cash, self.emergency_cash_floor)):
            raise ValueError("Reserves, available cash, and floor must be nonnegative")
        if not 0 <= self.reinvestment_rate <= 1:
            raise ValueError("Reinvestment share must lie in [0, 1]")

    def budget(self) -> dict:
        """Only POSITIVE realized gains after operational costs and tax allowance.

        Calculated hardware/research budget is capped by uncommitted cash
        above the emergency floor. A negative or zero profit gives zero.
        """
        distributable = max(0.0, self.after_cost_realized_profit - self.tax_reserve)
        liquidity_excess = max(0.0, self.available_liquid_cash-self.emergency_cash_floor)
        authorized = min(distributable*self.reinvestment_rate, liquidity_excess)
        return {**asdict(self), "available_for_reinvestment": round(authorized,2),
                "status": "PLANNING_ONLY_REQUIRES_HUMAN_APPROVAL"}
