"""Causal, rule-based volatility shock and drawdown exposure throttling.

No AI oracle: uses only *completed* market bars. No strategy may increase
risk caps because it believes an unusually large profit is imminent.
"""
from __future__ import annotations
from math import log, sqrt
from statistics import mean, pstdev
from .data import Bar


def regime_multiplier(histories:dict[str,list[Bar]], *, fast:int=24,
                      slow:int=120, stress_ratio:float=1.8,
                      market_drawdown:float=.10) -> dict:
    if fast<8 or slow<=fast or stress_ratio<=1 or not 0<market_drawdown<1:
        raise ValueError('Invalid market regime configuration')
    if not histories:
        raise ValueError('No market histories')
    if any(len(h)<slow+1 for h in histories.values()):
        return {'multiplier':.5,'regime':'INSUFFICIENT_HISTORY','volatility_ratio':None,
                'market_drawdown':None}
    ratios=[];drawdowns=[]
    for s,bars in histories.items():
        c=[b.close for b in bars[-slow-1:]]
        ret=[log(c[i]/c[i-1]) for i in range(1,len(c))]
        fast_vol=max(1e-8,pstdev(ret[-fast:]))
        slow_vol=max(1e-8,pstdev(ret))
        ratios.append(fast_vol/slow_vol)
        drawdowns.append(max(0,1-c[-1]/max(c[-slow:])))
    vol_ratio=mean(ratios)
    dd=mean(drawdowns)
    if vol_ratio>=stress_ratio or dd>=market_drawdown:
        mult=.4;name='STRESSED'
    elif vol_ratio>=1.3 or dd>=market_drawdown/2:
        mult=.7;name='CAUTION'
    else:
        mult=1.;name='NORMAL'
    return {'multiplier':mult,'regime':name,'volatility_ratio':vol_ratio,
            'market_drawdown':dd}
