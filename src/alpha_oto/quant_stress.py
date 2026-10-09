"""Cost sensitivity and uncertainty diagnostics. Never forecasts expected profit."""
from __future__ import annotations
from dataclasses import replace
from pathlib import Path
import json
from .portfolio import PortfolioConfig, load_universe, simulate_portfolio, timeline_for
from .quant_agents import QuantAgent
from .quant_validation import moving_block_ci


def stress_test(files, *, kind='trend', fast=48, slow=168,
                config:PortfolioConfig=PortfolioConfig(),
                out='artifacts/omega/stress.json'):
    universe=load_universe(files)
    times=timeline_for(universe,config)
    if len(times)<max(720,slow*4):
        raise ValueError('Insufficient observations for sensitivity test')
    agent=QuantAgent(kind,fast,slow)
    # The stress windows are not new validation sets. Never use these
    # comparisons to tune against an already inspected historical holdout.
    variants={
      'baseline':config,
      'double_fees':replace(config,side_fee_bps=2*config.side_fee_bps),
      'triple_slippage':replace(config,side_slippage_bps=3*config.side_slippage_bps),
      'low_liquidity':replace(config,max_participation=max(1e-5,config.max_participation/5)),
      'half_risk':replace(config,max_gross=config.max_gross/2,
                          max_asset=config.max_asset/2,
                          target_bar_vol=config.target_bar_vol/2),
    }
    results={}
    for name,settings in variants.items():
        strategy=simulate_portfolio(universe,agent,settings)
        reference=simulate_portfolio(universe,config=settings,benchmark='equal_weight',first=slow+1)
        returns=[b['equity']/a['equity']-1 for a,b in zip(strategy.equity_curve,strategy.equity_curve[1:])]
        ci=moving_block_ci(returns,block=min(24,len(returns)),resamples=400)
        results[name]={'agent':strategy.summary(),'equal_weight_reference':reference.summary(),
                       'per_bar_return_uncertainty':ci}
    result={'status':'RESEARCH_ONLY_NO_LIVE_TRADES','asset_count':len(universe),
            'strategy':agent.name,'variants':results,
            'warning':'Stress scenarios are retrospective, not profit guarantees. Existing test windows are no longer fresh.'}
    target=Path(out)
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    return result
