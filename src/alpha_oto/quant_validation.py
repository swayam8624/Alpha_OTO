"""Time-ordered agent competition with cost stress and moving-block uncertainty.

The validation periods are DISJOINT. Model selection never reads the final
holdout labels until the selected configuration is fixed. This is an initial
research diagnostic, not a statistical claim of market profitability.
"""
from __future__ import annotations
from dataclasses import replace
from math import isfinite, sqrt
from random import Random
from statistics import mean
from pathlib import Path
from hashlib import sha256
from .quant_agents import QuantAgent, agents_default
from .portfolio import PortfolioConfig, load_universe, timeline_for, simulate_portfolio
from .data import read_csv


def splits(n:int, *, warmup:int) -> tuple[list[tuple[int,int]],tuple[int,int]]:
    if n<max(720,warmup*4):
        raise ValueError('Need >=720 aligned observations and adequate warmup')
    bound=[int(f*n) for f in (.44,.56,.68,.80)]
    if bound[0]<=warmup or any(b-a<40 for a,b in zip(bound,bound[1:])):
        raise ValueError('Insufficient validation data')
    return [(bound[0],bound[1]),(bound[1],bound[2]),(bound[2],bound[3])],(bound[3],n)


def moving_block_ci(returns:list[float], *, block:int=24, resamples:int=400,
                    seed:int=42) -> dict:
    """Circular moving-block bootstrap: descriptive uncertainty, not true p-value.

    Bootstrap sampling captures short-range serial dependence approximately;
    it cannot account for market selection, covariate shift or strategy search.
    """
    if not returns or any(not isfinite(x) or x<=-1 for x in returns):
        raise ValueError('Bad return vector')
    if not 1<=block<=len(returns) or resamples<40:
        raise ValueError('Invalid block/bootstrap settings')
    rng=Random(seed)
    n=len(returns)
    means=[]
    for _ in range(resamples):
        sample=[]
        while len(sample)<n:
            start=rng.randrange(n)
            sample.extend(returns[(start+j)%n] for j in range(block))
        means.append(mean(sample[:n]))
    means.sort()
    return {'observed_mean_return_per_bar':mean(returns),
            'bootstrap_mean_ci_95':[means[int(.025*resamples)],
                                    means[min(resamples-1,int(.975*resamples))]],
            'n_bars':n,'block_bars':block,'resamples':resamples,
            'note':'Descriptive block bootstrap, does not correct for trying multiple agents.'}


def _daily_deltas(curve:list[dict]):
    return [b['equity']/a['equity']-1 for a,b in zip(curve,curve[1:])]


def _score(result, benchmark):
    # Preference for positive after-cost excess growth with drawdown and
    # excess turnover penalties. Predeclared, not fit to holdout.
    return (result.net_return-benchmark.net_return
            - .75*result.max_drawdown - .00005*result.turnover)


def research_portfolio(files:list[str | Path], output:str | Path,
                       *, config:PortfolioConfig=PortfolioConfig(),
                       candidates:tuple[QuantAgent,...]|None=None,
                       min_fills:int=12) -> dict:
    if min_fills<1:
        raise ValueError('Invalid minimum fills')
    universe=load_universe(files)
    if len(universe)<2:
        raise ValueError('A multi-asset experiment requires >=2 assets')
    candidates=candidates if candidates is not None else agents_default()
    if not candidates or len({a.name for a in candidates}) != len(candidates):
        raise ValueError('No agents or duplicate identities')
    times=timeline_for(universe,config)
    warmup=max(a.slow+1 for a in candidates)
    folds,holdout=splits(len(times),warmup=warmup)
    rows=[]
    for agent in candidates:
        periods=[]
        for begin,end in folds:
            res=simulate_portfolio(universe,agent,config,first=begin,last=end)
            baseline=simulate_portfolio(universe,config=config,first=begin,last=end,
                                        benchmark='equal_weight')
            periods.append({'start':times[begin].isoformat(),
                            'end_exclusive':(times[end-1]).isoformat(),
                            'agent':res.summary(),'benchmark':baseline.summary(),
                            'score':_score(res,baseline)})
        avg_score=mean(x['score'] for x in periods)
        positive=sum(x['agent']['net_return'] > x['benchmark']['net_return'] for x in periods)
        fill_count=sum(x['agent']['fills'] for x in periods)
        # A quiet strategy that happens to avoid drawdowns is not 'profitable'.
        eligible=positive>=2 and fill_count>=min_fills and avg_score>0
        rows.append({'agent':agent.name,'average_excess_risk_score':avg_score,
                     'positive_excess_folds':positive,'validation_fills':fill_count,
                     'shadow_eligible':eligible,'folds':periods})
    rows.sort(key=lambda x:x['average_excess_risk_score'],reverse=True)
    eligible=next((x for x in rows if x['shadow_eligible']),None)
    # A candidate is selected ONLY if its validation evidence meets predeclared
    # minimum. Without a qualifying agent, the holdout remains unqueried.
    chosen=next((a for a in candidates if eligible and a.name==eligible['agent']),None)
    out={'status':'RESEARCH_ONLY_NO_BROKER_ORDERS',
         'assets':sorted(universe),
         'data_sha256':{read_csv(p)[0].symbol:sha256(Path(p).read_bytes()).hexdigest() for p in files},
         'selection_rule':'2/3 positive excess-return validation folds, >=min_fills, positive mean penalized excess score',
         'validation':rows,'chosen_shadow_agent':chosen.name if chosen else None,
         'holdout':None,'warning':'Do not optimize further using this same heldout period.'}
    if chosen:
        begin,end=holdout
        res=simulate_portfolio(universe,chosen,config,first=begin,last=end)
        baseline=simulate_portfolio(universe,config=config,first=begin,last=end,
                                    benchmark='equal_weight')
        agent_r=_daily_deltas(res.equity_curve)
        base_r=_daily_deltas(baseline.equity_curve)
        diff=[a-b for a,b in zip(agent_r,base_r)]
        out['holdout']={'agent':res.summary(),'benchmark':baseline.summary(),
                         'excess_net_return':res.net_return-baseline.net_return,
                         'excess_return_uncertainty':moving_block_ci(diff,block=min(24,len(diff)))}
    target=Path(output)
    target.parent.mkdir(parents=True,exist_ok=True)
    import json
    target.write_text(json.dumps(out,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    return out
