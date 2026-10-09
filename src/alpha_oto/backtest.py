"""Long-or-cash, next-open execution backtester with explicit friction.

Market-order assumptions are necessarily optimistic for illiquid securities.
OHLCV alone does not establish intrabar fillability or quote availability.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
from math import sqrt
from typing import Sequence
from .data import Bar, validate_series
from .strategies import Strategy


@dataclass(frozen=True)
class ExecutionSettings:
    starting_cash: float = 100_000
    position_fraction: float = 0.25
    side_fee_bps: float = 15.0
    side_slippage_bps: float = 10.0
    halt_drawdown: float = 0.12
    warmup: int = 31

    def __post_init__(self):
        if self.starting_cash <= 0 or not 0 < self.position_fraction <= 1:
            raise ValueError("Invalid starting capital or position fraction")
        if min(self.side_fee_bps, self.side_slippage_bps) < 0:
            raise ValueError("Costs must be nonnegative")
        if not 0 < self.halt_drawdown <= 1 or self.warmup < 1:
            raise ValueError("Invalid risk guard or warmup")


@dataclass(frozen=True)
class Fill:
    timestamp: str
    side: str
    shares: float
    price: float
    fee: float
    cash_after: float


@dataclass(frozen=True)
class Result:
    strategy: str
    start: str
    end: str
    initial_cash: float
    final_equity: float
    net_return: float
    max_drawdown: float
    annualized_sharpe: float | None
    round_trips: int
    total_fees: float
    halted: bool
    fills: tuple[Fill, ...]
    equity_curve: tuple[tuple[str, float], ...]

    def summary(self) -> dict:
        data = asdict(self)
        data["fills"] = len(self.fills)
        data["equity_curve"] = len(self.equity_curve)
        return data


def simulate(bars: list[Bar], strategy: Strategy, settings: ExecutionSettings = ExecutionSettings(),
             *, evaluation_begin: int | None = None, evaluation_end: int | None = None,
             periods_per_year: float = 252.0) -> Result:
    """Evaluate on [evaluation_begin, evaluation_end); use earlier bars as context.

    Decision uses only bars up to the previous CLOSE. Execution happens at
    next bar OPEN + adverse slippage. Trades do not occur on the same bar as
    the signal. Fractional shares are simulated; round to lots in live systems.
    """
    validate_series(bars)
    begin = settings.warmup if evaluation_begin is None else evaluation_begin
    end = len(bars) if evaluation_end is None else evaluation_end
    if not settings.warmup <= begin < end <= len(bars) or end-begin < 3:
        raise ValueError("Invalid evaluation interval / insufficient observations")
    cash = settings.starting_cash
    shares = 0.0
    fees = 0.0
    fills: list[Fill] = []
    points: list[tuple[str, float]] = []
    peak = cash
    max_dd = 0.0
    halted = False
    trips = 0
    last_equity = cash
    changes: list[float] = []

    def transact(timestamp: str, side: str, price: float, quantity: float) -> None:
        nonlocal cash, shares, fees, trips
        fee = price*quantity*settings.side_fee_bps/10000
        if side == "BUY":
            cash -= price*quantity + fee
            shares += quantity
        else:
            cash += price*quantity - fee
            shares -= quantity
            trips += 1
        fees += fee
        fills.append(Fill(timestamp, side, quantity, price, fee, cash))

    for t in range(begin, end):
        bar = bars[t]
        # The strategy never receives bar[t] (or any future bar).
        history: Sequence[Bar] = tuple(bars[:t])
        requested = bool(strategy.decide(history)) if not halted else False
        if requested and shares == 0:
            price = bar.open*(1 + settings.side_slippage_bps/10000)
            target_notional = min(cash, settings.starting_cash*settings.position_fraction)
            qty = target_notional/(price*(1 + settings.side_fee_bps/10000))
            if qty > 0:
                transact(bar.timestamp.isoformat(), "BUY", price, qty)
        elif not requested and shares > 0:
            price = bar.open*(1 - settings.side_slippage_bps/10000)
            transact(bar.timestamp.isoformat(), "SELL", price, shares)
        equity = cash + shares*bar.close
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak-equity)/peak)
        if max_dd >= settings.halt_drawdown:
            halted = True
        if last_equity > 0:
            changes.append(equity/last_equity-1)
        last_equity = equity
        points.append((bar.timestamp.isoformat(), equity))

    # Final liquidation is priced conservatively at the last observed CLOSE.
    # This is a reporting convention, not proof the closing quote was executable.
    if shares > 0:
        bar = bars[end-1]
        price = bar.close*(1 - settings.side_slippage_bps/10000)
        transact(bar.timestamp.isoformat(), "SELL", price, shares)
        last_equity = cash
        points[-1] = (bar.timestamp.isoformat(), cash)
        peak = max(peak, cash)
        max_dd = max(max_dd, (peak-cash)/peak)
    sharpe = None
    if len(changes) > 2:
        avg = sum(changes)/len(changes)
        variance = sum((x-avg)**2 for x in changes)/(len(changes)-1)
        if variance > 1e-18:
            sharpe = avg/sqrt(variance)*sqrt(periods_per_year)
    return Result(strategy.name, bars[begin].timestamp.isoformat(), bars[end-1].timestamp.isoformat(),
                  settings.starting_cash, last_equity, last_equity/settings.starting_cash-1,
                  max_dd, sharpe, trips, fees, halted, tuple(fills), tuple(points))
