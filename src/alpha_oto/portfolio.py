"""Synchronous, timestamp-safe, long-only multi-asset simulation with cash.

Works on historical 24/7 markets whose completed bars share a frequency.
Missing market observations are never filled; we carry only last known marks
with a conspicuous stale-pricing flag, and BLOCK ALL trades on stale ticks.
Bar t is executed at t.open from signals through t-1 only (optimistic boundary
latency assumption). Close-to-close portfolio NAV is recorded each bar.
Not suitable for live order execution, derivatives, or non-24/7 session markets.
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from math import isfinite, sqrt
from pathlib import Path
from typing import Mapping, Sequence
from .data import Bar, read_csv, validate_series
from .quant_agents import QuantAgent, allocate_scores, realized_vol
from .covariance import covariance_allocator
from .regimes import regime_multiplier


@dataclass(frozen=True)
class PortfolioConfig:
    initial_cash: float = 100_000.0
    side_fee_bps: float = 15.0
    side_slippage_bps: float = 10.0
    max_gross: float = .85
    max_asset: float = .45
    target_bar_vol: float = .012
    max_drawdown: float = .15
    rebalance_bars: int = 24
    min_trade_notional: float = 100.0
    max_participation: float = .01
    interval_seconds: int = 3600
    allocation_mode: str = "inverse_vol"
    regime_control: bool = True

    def __post_init__(self):
        if self.allocation_mode not in ("inverse_vol", "risk_parity"):
            raise ValueError("Unsupported allocator")
        if not all(isfinite(x) for x in (self.initial_cash,self.side_fee_bps,
                self.side_slippage_bps,self.max_gross,self.max_asset,
                self.target_bar_vol,self.max_drawdown,self.min_trade_notional,
                self.max_participation)):
            raise ValueError('All cost/risk parameters must be finite')
        if self.initial_cash <= 0 or min(self.side_fee_bps,self.side_slippage_bps) < 0:
            raise ValueError('Invalid funds or costs')
        if not (0 < self.max_gross <= 1 and 0 < self.max_asset <= 1
                and 0 < self.target_bar_vol < 1 and 0 < self.max_drawdown <= 1):
            raise ValueError('Invalid risk caps')
        if not 0 < self.max_participation <= 1 or self.min_trade_notional < 0:
            raise ValueError('Invalid turnover/liquidity limits')
        if self.rebalance_bars < 1 or self.interval_seconds < 1:
            raise ValueError('Invalid scheduling')


@dataclass(frozen=True)
class Trade:
    timestamp: str
    symbol: str
    side: str
    units: float
    execution_price: float
    fee: float
    notional: float


@dataclass
class PortfolioResult:
    agent: str
    initial_cash: float
    final_equity: float
    net_return: float
    mark_to_market_equity: float
    estimated_final_exit_cost: float
    max_drawdown: float
    annualized_sharpe: float | None
    turnover: float
    fees: float
    fills: list[Trade]
    equity_curve: list[dict]
    stale_valuation_bars: int
    blocked_order_bars: int
    halted: bool
    final_positions: dict[str,float]

    def summary(self):
        data = asdict(self)
        data['fills'] = len(self.fills)
        data['equity_curve_length'] = len(self.equity_curve)
        data.pop('equity_curve')
        return data


def load_universe(files: Sequence[str | Path]) -> dict[str,list[Bar]]:
    if len(files) < 1:
        raise ValueError('At least one asset required')
    result={}
    for file in files:
        candles=read_csv(file)
        symbol=candles[0].symbol
        if symbol in result:
            raise ValueError('Duplicate symbol: '+symbol)
        result[symbol]=candles
    return result


def timeline_for(universe: Mapping[str, Sequence[Bar]], config: PortfolioConfig) -> list[datetime]:
    if not universe or any(not v for v in universe.values()):
        raise ValueError('Empty universe')
    currencies={s.rsplit('-',1)[-1] for s in universe}
    # Prevent the same cash balance from summing prices denominated in INR,
    # USD and USDT as though FX/settlement conversion had already occurred.
    if len(currencies)!=1 or next(iter(currencies)) not in {'USD','USDT','INR','EUR'}:
        raise ValueError('Incompatible or ambiguous quote currencies; convert with a separately licensed FX/settlement ledger')
    for bars in universe.values():
        validate_series(list(bars))
    # Require consistent UTC grid; never pretend stocks from different trading
    # calendars are 24/7 crypto. Per-market simulation requires calendar adapter.
    start=max(bars[0].timestamp for bars in universe.values())
    end=min(bars[-1].timestamp for bars in universe.values())
    if end <= start:
        raise ValueError('No overlapping timestamp range')
    dt=timedelta(seconds=config.interval_seconds)
    if any((b.timestamp - start).total_seconds() % config.interval_seconds != 0
           for bars in universe.values() for b in (bars[0],bars[-1])):
        raise ValueError('Assets not on the same candle grid')
    count=int((end-start).total_seconds()//config.interval_seconds) + 1
    if count > 500_000:
        raise ValueError('Dataset excessively large')
    return [start+i*dt for i in range(count)]


def _returns_sharpe(curve: list[dict], steps_year: float) -> float | None:
    rs=[cur['equity']/prev['equity']-1 for prev,cur in zip(curve,curve[1:])]
    if len(rs) < 4:
        return None
    avg=sum(rs)/len(rs)
    var=sum((r-avg)**2 for r in rs)/(len(rs)-1)
    return avg/sqrt(var)*sqrt(steps_year) if var > 1e-18 else None


def simulate_portfolio(universe: Mapping[str, Sequence[Bar]],
                       agent: QuantAgent | None = None,
                       config: PortfolioConfig = PortfolioConfig(),
                       *, first: int | None = None, last: int | None = None,
                       benchmark: str | None = None) -> PortfolioResult:
    """Simulate across aligned clocks; limit order size to bar volume fraction.

    benchmark = 'equal_weight' means buy and maintain equal long allocations
    via periodic rebalance; 'cash' means no orders. All strategies use SAME
    execution and fee/volume rules. Start with fresh independent cash per slice.
    """
    if benchmark not in (None,'equal_weight','cash'):
        raise ValueError('Invalid benchmark')
    if benchmark is None and agent is None:
        raise ValueError('Agent required')
    times=timeline_for(universe,config)
    warm=(agent.slow + 1) if agent is not None else 1
    begin=warm if first is None else first
    stop=len(times) if last is None else last
    if begin < warm or begin >= stop or stop>len(times) or stop-begin<5:
        raise ValueError('Invalid research interval')
    bar_maps={sym:{b.timestamp:b for b in bars} for sym,bars in universe.items()}
    symbols=sorted(bar_maps)
    positions={s:0.0 for s in symbols}
    cash=config.initial_cash
    fees=turnover=0.0
    fills=[]; curve=[]
    last_close:dict[str,float]={}
    # A continuous feature context must end at previous tick; missing data
    # resets eligibility until the strategy has slow+1 new consecutive bars.
    windows={s:deque(maxlen=max(warm,169)) for s in symbols}
    peak=config.initial_cash
    max_dd=0.0; halted=False
    stale_count=blocked_count=0
    prior_complete=True
    # Warm windows from start of common overlapping history. Data before
    # evaluation begin may inform indicators but NEVER generate opening trades.
    for idx,t in enumerate(times[:stop]):
        row={s:bar_maps[s].get(t) for s in symbols}
        if idx < begin:
            for s,b in row.items():
                if b is None:
                    windows[s].clear()
                else:
                    windows[s].append(b)
                    last_close[s]=b.close
            continue
        is_fresh=all(b is not None for b in row.values())
        # Entries/exits require full synchronized execution quotes. Partial
        # outages block ALL orders (fail-closed). Risk halt prevents new buys.
        # After a drawdown breach, do not buy again. On the next available
        # complete tick attempt to liquidate (volume participation still caps
        # fills). A halt should not strand a position indefinitely.
        can_rebalance=(is_fresh and
                       (halted or (prior_complete and (idx-begin) % config.rebalance_bars == 0)))
        if is_fresh and not halted and not prior_complete:
            blocked_count+=1
        if can_rebalance:
            if halted or benchmark=='cash':
                target={s:0.0 for s in symbols}
            elif benchmark=='equal_weight':
                w=min(config.max_asset,config.max_gross/len(symbols))
                target={s:w for s in symbols}
            else:
                scores={}; vol={}
                histories={s:list(windows[s]) for s in symbols}
                if hasattr(agent,'scores_universe'):
                    scores=agent.scores_universe(histories)
                    if set(scores)!=set(symbols):
                        raise ValueError('ML scorer must cover every symbol')
                for s in symbols:
                    hist=histories[s]
                    if len(hist)<agent.slow+1:
                        scores[s]=0.0; vol[s]=.01
                    else:
                        if not hasattr(agent,'scores_universe'):
                            scores[s]=agent.score(hist)
                        vol[s]=realized_vol(hist,min(agent.lookback,48))
                if config.allocation_mode == 'risk_parity' and all(len(h)>=49 for h in histories.values()):
                    target=covariance_allocator(histories,scores,
                        gross_limit=config.max_gross,per_asset_limit=config.max_asset,
                        volatility_target=config.target_bar_vol,lookback=48)
                else:
                    target=allocate_scores(scores,vol,gross_limit=config.max_gross,
                                           per_asset_limit=config.max_asset,
                                           volatility_target=config.target_bar_vol)
            # Rule-based regime detector may only THROTTLE requested exposure.
            # It cannot relax explicit risk caps. Must use history ending t-1.
            if config.regime_control and not halted and benchmark!='cash':
                states=regime_multiplier({s:list(windows[s]) for s in symbols})
                factor=states['multiplier']
                target={s:w*factor for s,w in target.items()}
            # Compute all target values from a single pre-trade NAV, at market
            # OPEN before any transaction; first sell surplus, then buy.
            pre_value=cash+sum(positions[s]*row[s].open for s in symbols)
            for side in ('SELL','BUY'):
                for s in symbols:
                    b=row[s]
                    px=b.open*(1+config.side_slippage_bps/1e4) if side=='BUY' else b.open*(1-config.side_slippage_bps/1e4)
                    have=positions[s]*b.open
                    wanted=target[s]*pre_value
                    diff=(wanted-have) if side=='BUY' else (have-wanted)
                    if diff < config.min_trade_notional:
                        continue
                    # The current bar's *total* volume is unknown at its OPEN.
                    # Use only previous completed candle volume as a rough
                    # capacity proxy. This is not broker order-book depth.
                    prior_volume=windows[s][-1].volume if windows[s] else 0.0
                    max_value=min(diff, config.max_participation*prior_volume*b.open)
                    if side=='BUY':
                        max_value=min(max_value,cash/(1+config.side_fee_bps/1e4))
                    else:
                        max_value=min(max_value,positions[s]*px)
                    if max_value < config.min_trade_notional:
                        continue
                    qty=max_value/px
                    fee=max_value*config.side_fee_bps/1e4
                    if side=='BUY':
                        cash-=max_value+fee; positions[s]+=qty
                    else:
                        cash+=max_value-fee; positions[s]-=qty
                        positions[s]=max(0,positions[s])
                    turnover+=max_value; fees+=fee
                    fills.append(Trade(t.isoformat(),s,side,qty,px,fee,max_value))
        for s,b in row.items():
            if b is None:
                windows[s].clear()
            else:
                windows[s].append(b)
                last_close[s]=b.close
        prior_complete=is_fresh
        if not is_fresh:
            stale_count+=1
        # stale last-close NAV is informational only; these results cannot
        # prove tradability during outages.
        nav=cash+sum(positions[s]*last_close.get(s,0) for s in symbols)
        if nav <= 0 or not isfinite(nav):
            raise ValueError('Insolvent/nonfinite NAV in backtest')
        peak=max(peak,nav)
        max_dd=max(max_dd,1-nav/peak)
        if max_dd>=config.max_drawdown:
            halted=True
        curve.append({'timestamp':t.isoformat(), 'equity':nav,'stale':not is_fresh,
                      'cash':cash,'gross_exposure':sum(positions[s]*last_close.get(s,0) for s in symbols)/nav})
    # Estimate full liquidation at final observed closes with adverse spread
    # and fees. This is NOT an actual filled liquidation, and is optimistic
    # during a stale final bar or when the required quantity exceeds depth.
    pending_exit_cost=sum(positions[s]*last_close.get(s,0) *
                    (config.side_fee_bps+config.side_slippage_bps)/10_000 for s in symbols)
    estimated_final=nav-pending_exit_cost
    return PortfolioResult(agent.name if agent else benchmark,config.initial_cash,estimated_final,
                           estimated_final/config.initial_cash-1,nav,pending_exit_cost,max_dd,
                           _returns_sharpe(curve,365*24*3600/config.interval_seconds),
                           turnover/config.initial_cash,fees,fills,curve,stale_count,
                           blocked_count,halted,positions)
