"""Sample-covariance shrinkage and constrained covariance risk budgeting.

A deliberately inspectable risk-model baseline; estimates are uncertain, and
past correlations may change abruptly. Not a portfolio-profit guarantee.
"""
from __future__ import annotations
from math import log, sqrt, isfinite
from .data import Bar


def shrink_covariance(histories:dict[str,list[Bar]], *, lookback:int=48,
                      shrinkage:float=.55) -> tuple[list[str],list[list[float]]]:
    if lookback<8 or not 0<=shrinkage<=1 or len(histories)<2:
        raise ValueError('Invalid covariance settings')
    syms=sorted(histories)
    if any(len(histories[s])<lookback+1 for s in syms):
        raise ValueError('Insufficient covariance history')
    window={s:histories[s][-(lookback+1):] for s in syms}
    first=window[syms[0]]
    if any([b.timestamp for b in window[s]] != [b.timestamp for b in first] for s in syms[1:]):
        raise ValueError('Cross-asset covariance requires aligned timestamps')
    rs=[[log(v[i].close/v[i-1].close) for i in range(1,len(v))]
        for v in (window[s] for s in syms)]
    means=[sum(a)/len(a) for a in rs]
    m=len(syms)
    cov=[[sum((rs[i][t]-means[i])*(rs[j][t]-means[j]) for t in range(lookback)) /
          max(1,lookback-1) for j in range(m)] for i in range(m)]
    diag=[max(cov[i][i],1e-10) for i in range(m)]
    for i in range(m):
        for j in range(m):
            if i==j: cov[i][j]=diag[i]
            else: cov[i][j]*=(1-shrinkage)
    return syms,cov


def portfolio_variance(weights:list[float], covariance:list[list[float]]) -> float:
    if len(weights)!=len(covariance) or any(len(row)!=len(weights) for row in covariance):
        raise ValueError('Mismatched covariance shape')
    result=sum(weights[i]*weights[j]*covariance[i][j]
               for i in range(len(weights)) for j in range(len(weights)))
    return max(0,result)


def covariance_allocator(histories:dict[str,list[Bar]], scores:dict[str,float],
                         *, gross_limit:float=.85, per_asset_limit:float=.45,
                         volatility_target:float=.012,lookback:int=48,
                         shrinkage:float=.55) -> dict[str,float]:
    if set(histories)!=set(scores) or not 0<gross_limit<=1 or not 0<per_asset_limit<=1 or volatility_target<=0:
        raise ValueError('Invalid allocation inputs')
    if any(not isfinite(v) for v in scores.values()):
        raise ValueError('Nonfinite agent signal')
    syms,cov=shrink_covariance(histories,lookback=lookback,shrinkage=shrinkage)
    active=[i for i,s in enumerate(syms) if scores[s]>0]
    if not active:
        return {s:0. for s in syms}
    # Capped, signal-proportional initial allocation.
    weights=[0.]*len(syms)
    for i in active:
        weights[i]=(min(2.,max(.1,scores[syms[i]]))/sqrt(cov[i][i]))
    base=sum(weights)
    weights=[min(per_asset_limit,gross_limit*w/base) for w in weights]
    # Iteratively equalize positive marginal covariance risk contributions
    # among active assets while retaining score tilt. Not a convex optimizer;
    # convergence quality is reported as a model limitation.
    targets=[sqrt(min(2.,max(.1,scores[syms[i]]))) if i in active else 0 for i in range(len(syms))]
    target_total=sum(targets)
    targets=[x/target_total for x in targets]
    for _ in range(40):
        risk=[sum(cov[i][j]*weights[j] for j in range(len(syms))) for i in range(len(syms))]
        contrib=[max(1e-12,weights[i]*risk[i]) if i in active else 0 for i in range(len(syms))]
        total=sum(contrib)
        if total<=1e-12:
            break
        candidate=[]
        for i,w in enumerate(weights):
            if i not in active:
                candidate.append(0.);continue
            ratio=max(.05,min(20.,targets[i]*total/contrib[i]))
            candidate.append(w*sqrt(ratio))
        new_total=sum(candidate)
        weights=[min(per_asset_limit,gross_limit*w/new_total) for w in candidate]
    realized=sqrt(portfolio_variance(weights,cov))
    if realized>volatility_target:
        scale=volatility_target/realized
        weights=[w*scale for w in weights]
    return {s:max(0.,w) for s,w in zip(syms,weights)}
