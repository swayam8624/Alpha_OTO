"""Stateful local online-learning quant committee, SHADOW research only.

Evidence-based exponential weighting is NOT a magic evolutionary trader.
Rewards are proxy returns on previous signals, not confirmed broker P&L.
On each portfolio rebalance it updates ONLY from fully completed price bars.
Data gaps freeze learning instead of penalizing agents for unknown outcomes.
"""
from __future__ import annotations
from dataclasses import dataclass,field
from math import exp, log, isfinite, sqrt
from statistics import mean
from pathlib import Path
import json
from .data import Bar
from .portfolio import PortfolioConfig,load_universe,simulate_portfolio,timeline_for
from .quant_agents import QuantAgent,agents_default
from .quant_validation import splits,_score

# Scores have different natural scales; predeclared denominators keep one family
# from dominating merely because a z-score is numerically larger than a return.
SCALE={'trend':.015,'momentum':.05,'reversal':.75,
       'breakout':.015,'vol_adjusted_momentum':.8,'defensive':.8}


def _strength(agent:QuantAgent, bars:list[Bar]) -> float:
    raw=agent.score(bars)
    return max(0.,min(1.,raw/SCALE[agent.kind]))


@dataclass
class AdaptiveCommittee:
    """Cost-aware online learning for research. No account access.

    An expert receives reward only after the next completed observation. The
    learning statistic is lagged *price-direction proxy*, not an executable
    trading profit: real execution costs and risk appear in portfolio replay.
    """
    experts:tuple[QuantAgent,...]=field(default_factory=agents_default)
    eta:float=2.0
    quarantine_drawdown:float=.07
    min_updates:int=12
    proxy_side_cost_bps:float=25.0
    slow:int=168
    lookback:int=48
    log_weights:list[float]=field(default_factory=list)
    previous_strengths:dict[str,list[float]]=field(default_factory=dict)
    previous_closes:dict[str,float]=field(default_factory=dict)
    previous_timestamp:object=None
    updates:int=0
    cumulative_proxy:list[float]=field(default_factory=list)
    observation_tally:list[int]=field(default_factory=list)
    quarantined:set[int]=field(default_factory=set)
    last_weights:list[float]=field(default_factory=list)

    def __post_init__(self):
        if (not self.experts or not 0<self.eta<=20
                or not 0<self.quarantine_drawdown<1 or self.min_updates<3
                or not 0<=self.proxy_side_cost_bps<500):
            raise ValueError('Invalid online swarm configuration')
        if len({a.name for a in self.experts})!=len(self.experts):
            raise ValueError('Duplicate expert identities')
        n=len(self.experts)
        self.log_weights=[0.]*n
        self.cumulative_proxy=[0.]*n
        self.observation_tally=[0]*n
        self.last_weights=[1/n]*n
        self.slow=max(self.slow,max(a.slow for a in self.experts))

    @property
    def name(self):
        return f'local_adaptive_committee_eta{self.eta:g}'

    def _weights(self):
        active=[i for i in range(len(self.experts)) if i not in self.quarantined]
        if not active:
            # Never force a losing committee to trade. Inactive agents remain
            # in quarantine until fresh proxy evidence improves their score.
            return [0.]*len(self.experts)
        mx=max(self.log_weights[i] for i in active)
        probs=[exp(max(-20,min(20,self.log_weights[i]-mx))) if i in active else 0.
               for i in range(len(self.experts))]
        z=sum(probs)
        return [v/z for v in probs]

    def scores_universe(self, histories:dict[str,list[Bar]]) -> dict[str,float]:
        symbols=sorted(histories)
        if not symbols:
            raise ValueError('No market inputs')
        stamps={histories[s][-1].timestamp for s in symbols if histories[s]}
        if len(stamps)!=1 or any(len(histories[s])<self.slow+1 for s in symbols):
            # In particular, do not use a stale reference price from another
            # asset to update online weights after a broken market-data feed.
            self.previous_strengths={}
            self.previous_closes={}
            self.previous_timestamp=None
            return {s:0. for s in symbols}
        when=next(iter(stamps))
        if self.previous_timestamp is not None and when <= self.previous_timestamp:
            raise ValueError('Swarm input timestamps must strictly advance')
        # Current strengths use only the latest *completed* bar. They are
        # allowed to penalize changes in the previous exposure proxy, but not
        # to change the historical gain/loss accrued under the previous vote.
        strengths={s:[_strength(a,histories[s]) for a in self.experts]
                   for s in symbols}
        if self.previous_strengths and self.previous_closes:
            n=len(self.experts)
            rewards=[0.]*n
            for s in symbols:
                p0=self.previous_closes.get(s)
                if p0 is None or p0<=0:continue
                change=histories[s][-1].close/p0-1
                for i in range(n):
                    v=self.previous_strengths[s][i]
                    # One-side turnover friction applies to the change in
                    # exposure at the new decision. This is a proxy only;
                    # portfolio replay has its own independent fills/costs.
                    turnover=abs(strengths[s][i]-v)
                    rewards[i]+=(v*change - turnover*self.proxy_side_cost_bps/1e4)/len(symbols)
            for i,r in enumerate(rewards):
                self.cumulative_proxy[i]+=r
                self.observation_tally[i]+=1
                self.log_weights[i]=max(-10,min(10,self.log_weights[i]+self.eta*r))
                if self.observation_tally[i]>=self.min_updates:
                    # Quarantine needs sustained, materially negative evidence
                    # rather than a run of two or three isolated losing bars.
                    if self.cumulative_proxy[i] < -self.quarantine_drawdown:
                        self.quarantined.add(i)
                    elif self.cumulative_proxy[i] > -self.quarantine_drawdown/2:
                        self.quarantined.discard(i)
            self.updates+=1
        w=self._weights()
        self.last_weights=w[:]
        result={s:sum(w[i]*strengths[s][i] for i in range(len(self.experts)))
                for s in symbols}
        self.previous_closes={s:histories[s][-1].close for s in symbols}
        self.previous_strengths=strengths
        self.previous_timestamp=when
        return result

    def diagnostic(self) -> dict:
        return {'updates':self.updates,
                'weights':{a.name:round(self.last_weights[i],6)
                           for i,a in enumerate(self.experts)},
                'quarantined':[self.experts[i].name for i in sorted(self.quarantined)],
                'cumulative_proxy_not_trading_pnl':{
                    a.name:round(self.cumulative_proxy[i],6)
                    for i,a in enumerate(self.experts)}}


def swarm_research(files:list[str|Path],out:str|Path,
                   *,etas:tuple[float,...]=(.5,2.0,6.),
                   config:PortfolioConfig=PortfolioConfig(),
                   min_validation_fills:int=12):
    universe=load_universe(files)
    times=timeline_for(universe,config)
    folds,test=splits(len(times),warmup=169)
    candidates=[]
    for eta in etas:
        fold_results=[]
        for begin,end in folds:
            agent=AdaptiveCommittee(eta=eta)
            trial=simulate_portfolio(universe,agent,config,first=begin,last=end)
            ref=simulate_portfolio(universe,config=config,first=begin,last=end,
                                    benchmark='equal_weight')
            fold_results.append({'agent':trial.summary(),'reference':ref.summary(),
                                 'score':_score(trial,ref),
                                 'swarm':agent.diagnostic()})
        positive=sum(x['agent']['net_return']>x['reference']['net_return'] for x in fold_results)
        fills=sum(x['agent']['fills'] for x in fold_results)
        sc=mean(x['score'] for x in fold_results)
        candidates.append({'eta':eta,'mean_score':sc,'positive_excess_folds':positive,
                           'validation_fills':fills,
                           'shadow_eligible':positive>=2 and fills>=min_validation_fills and sc>0,
                           'folds':fold_results})
    best=next((a for a in sorted(candidates,key=lambda a:a['mean_score'],reverse=True)
               if a['shadow_eligible']),None)
    result={'status':'SHADOW_LEARNING_ONLY_NO_LIVE_ORDERS',
            'candidates':candidates,'selected_eta':best['eta'] if best else None,
            'holdout':None,
            'warning':'Expert proxy rewards are not realized trade P&L. After-cost portfolio replay is separate. Do not retune the same holdout.'}
    if best:
        agent=AdaptiveCommittee(eta=best['eta'])
        simulation=simulate_portfolio(universe,agent,config,first=test[0],last=test[1])
        ref=simulate_portfolio(universe,config=config,first=test[0],last=test[1],benchmark='equal_weight')
        result['holdout']={'agent':simulation.summary(),'reference':ref.summary(),
                           'swarm':agent.diagnostic(),
                           'excess_return':simulation.net_return-ref.net_return}
    dest=Path(out)
    dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    return result
