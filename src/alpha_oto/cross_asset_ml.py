"""Locally learned, pooled cross-asset forward-return regressors.

No clouds or LLMs. Fit only on labels whose target completes before the
training cutoff. Feature vector uses only completed, gap-free price history
from EVERY asset. Validation epochs are disjoint and never fit on their own
labels. Portfolio evaluator makes next-open, fee/slippage-aware decisions.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from math import log, isfinite
from pathlib import Path
from statistics import mean
import json
from .data import Bar, read_csv
from .quant_ml import LOOKBACK, feature_vector_at
from .portfolio import PortfolioConfig, load_universe, timeline_for, simulate_portfolio
from .quant_validation import _score


@dataclass(frozen=True)
class Row:
    decision_idx: int
    exit_idx: int
    symbol: str
    x: tuple[float,...]
    y: float


def _joint_features(histories:dict[str,list[Bar]], symbol:str) -> tuple[float,...]:
    own=histories[symbol]
    self_features=feature_vector_at(own,len(own)-1)
    companions=[histories[s] for s in sorted(histories) if s!=symbol]
    if not companions:
        raise ValueError('Cross-asset requires multiple markets')
    if any(len(b)<LOOKBACK or b[-1].timestamp != own[-1].timestamp
           for b in companions):
        raise ValueError('Unsynchronized cross-market information')
    others_1=mean(b[-1].close/b[-2].close-1 for b in companions)
    others_24=mean(b[-1].close/b[-25].close-1 for b in companions)
    spread=own[-1].close/own[-25].close-1-others_24
    return (*self_features,others_1,others_24,spread)


def _has_all(bmap:dict, times:list, start:int,end:int) -> bool:
    return all(t in bmap for t in times[start:end+1])


def prepare_rows(universe, config:PortfolioConfig, *, horizon:int=4):
    if not 1<=horizon<=72:
        raise ValueError('Horizon must be 1–72 candles')
    times=timeline_for(universe,config)
    bmaps={s:{b.timestamp:b for b in bs} for s,bs in universe.items()}
    rows=[]
    skipped=0
    for t in range(LOOKBACK-1,len(times)-horizon-1):
        if not all(_has_all(bmap,times,t-LOOKBACK+1,t) for bmap in bmaps.values()):
            skipped+=len(bmaps);continue
        hist={s:[bmap[times[i]] for i in range(t-LOOKBACK+1,t+1)] for s,bmap in bmaps.items()}
        for s,bmap in bmaps.items():
            entry=t+1;exit_=entry+horizon
            if not _has_all(bmap,times,entry,exit_):
                skipped+=1;continue
            p0=bmap[times[entry]].open
            p1=bmap[times[exit_]].open
            gross=log(p1/p0)
            # Targets expressed as cost-net log returns; costs are assumptions.
            cost=2*(config.side_fee_bps+config.side_slippage_bps)/10000
            rows.append(Row(t,exit_,s,_joint_features(hist,s),gross-cost))
    return rows,{'samples':len(rows),'skipped_gap_windows':skipped,'horizon_bars':horizon}


def build_regressor(name:str):
    if name=='ridge':
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        from sklearn.linear_model import Ridge
        return make_pipeline(StandardScaler(),Ridge(alpha=100.0))
    if name=='histgb':
        from sklearn.ensemble import HistGradientBoostingRegressor
        return HistGradientBoostingRegressor(max_iter=90,max_leaf_nodes=7,
                    min_samples_leaf=40, l2_regularization=30,
                    learning_rate=.035,random_state=42)
    if name=='lightgbm':
        from lightgbm import LGBMRegressor
        return LGBMRegressor(n_estimators=100,num_leaves=7,max_depth=3,
              min_child_samples=40,reg_lambda=30,learning_rate=.035,
              random_state=42,n_jobs=2,verbosity=-1)
    if name=='xgboost':
        from xgboost import XGBRegressor
        return XGBRegressor(n_estimators=100,max_depth=3,reg_lambda=30,
             learning_rate=.035,subsample=.9,colsample_bytree=.9,
             random_state=42,n_jobs=2)
    raise ValueError('Invalid regression model')


def fit_regressor(name, rows):
    if len(rows)<150:
        raise ValueError('Insufficient samples for fitting')
    model=build_regressor(name)
    model.fit([r.x for r in rows],[r.y for r in rows])
    return model


@dataclass
class PooledModelAgent:
    model: object
    kind: str
    horizon: int
    min_net_edge: float=.002
    lookback: int=48
    slow: int=48

    @property
    def name(self):
        return f'pooled_{self.kind}_h{self.horizon}_edge{self.min_net_edge:.4f}'

    def scores_universe(self,histories:dict[str,list[Bar]]):
        # Model must see no future. A gap in ANY cross-market window means all
        # predictions fail closed, rather than implicit forward-fill.
        if any(len(b)<LOOKBACK for b in histories.values()):
            return {s:0.0 for s in histories}
        if len({b[-1].timestamp for b in histories.values()})!=1:
            return {s:0.0 for s in histories}
        import numpy as np
        x=[_joint_features(histories,s) for s in sorted(histories)]
        estimates=self.model.predict(np.asarray(x,dtype=float))
        return {s:max(0.,float(v)-self.min_net_edge) if isfinite(v) else 0.0
                for s,v in zip(sorted(histories),estimates)}


def research_cross_asset(files:list[str|Path],outdir:str|Path,
                        *,config:PortfolioConfig=PortfolioConfig(),
                        models:tuple[str,...]=('ridge','histgb'),
                        horizons:tuple[int,...]=(4,12),
                        edge_thresholds:tuple[float,...]=(.001,.003),
                        min_validation_fills:int=12) -> dict:
    import joblib
    if not models or not horizons or not edge_thresholds:
        raise ValueError('Specify nonempty research grid')
    universe=load_universe(files)
    if len(universe)<2:
        raise ValueError('Require >=2 assets')
    times=timeline_for(universe,config)
    if len(times)<900:
        raise ValueError('Require at least 900 aligned hourly observations')
    fold_boundaries=[(int(.50*len(times)),int(.60*len(times))),
                     (int(.60*len(times)),int(.70*len(times))),
                     (int(.70*len(times)),int(.80*len(times)))]
    holdout=int(.80*len(times))
    reports=[];winner=None;winner_score=float('-inf')
    data_rows={h:prepare_rows(universe,config,horizon=h) for h in horizons}
    for horizon in horizons:
        rows,info=data_rows[horizon]
        for name in models:
            fold_cache=[]
            for begin,end in fold_boundaries:
                train=[r for r in rows if r.exit_idx<begin]
                if len(train)<150:
                    raise ValueError('Insufficient gap-safe training rows')
                model=fit_regressor(name,train)
                fold_cache.append((model,begin,end))
            for edge in edge_thresholds:
                if not 0<=edge<=.1:
                    raise ValueError('Invalid edge threshold')
                folds=[]
                for model,begin,end in fold_cache:
                    agent=PooledModelAgent(model,name,horizon,edge)
                    res=simulate_portfolio(universe,agent,config,first=begin,last=end)
                    base=simulate_portfolio(universe,config=config,first=begin,last=end,
                                            benchmark='equal_weight')
                    folds.append({'agent':res.summary(), 'benchmark':base.summary(),
                                  'score':_score(res,base)})
                positive=sum(x['agent']['net_return']>x['benchmark']['net_return'] for x in folds)
                total_fills=sum(x['agent']['fills'] for x in folds)
                sc=mean(x['score'] for x in folds)
                eligible=(positive>=2 and total_fills>=min_validation_fills and sc>0)
                item={'model':name,'horizon':horizon,'min_edge':edge,
                      'positive_excess_folds':positive,'validation_fills':total_fills,
                      'score':sc,'shadow_eligible':eligible,'folds':folds,
                      'gap_handling':info}
                reports.append(item)
                if eligible and sc>winner_score:
                    winner=item;winner_score=sc
    # Holdout is only read after the winning candidate is fixed, and only if
    # validation promotion criteria were met. Otherwise report 'not evaluated'.
    out={'status':'RESEARCH_ONLY_NOT_LIVE_APPROVED',
         'assets':sorted(universe),
         'holdout':'NOT_EVALUATED_NO_ELIGIBLE_CANDIDATE',
         'selected':({k:v for k,v in winner.items() if k!='folds'} if winner else None),
         'candidate_reports':reports,
         'data_sha256':{Path(p).name:sha256(Path(p).read_bytes()).hexdigest() for p in files},
         'warning':'Do not retune the grid on the same holdout. No inference of profit or executable fills.'}
    if winner:
        horizon=winner['horizon']
        rows,_=data_rows[horizon]
        trained=fit_regressor(winner['model'],[r for r in rows if r.exit_idx<holdout])
        agent=PooledModelAgent(trained,winner['model'],horizon,winner['min_edge'])
        res=simulate_portfolio(universe,agent,config,first=holdout,last=len(times))
        base=simulate_portfolio(universe,config=config,first=holdout,last=len(times),benchmark='equal_weight')
        out['holdout']={'agent':res.summary(),'benchmark':base.summary(),
                        'excess_return':res.net_return-base.net_return}
        # DO NOT allow model artifact to masquerade as approved production model.
        d=Path(outdir); d.mkdir(parents=True,exist_ok=True)
        joblib.dump(trained,d/'research_model_NOT_LIVE_APPROVED.joblib')
    target=Path(outdir)/'cross_asset_research.json'
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(out,indent=2,sort_keys=True)+'\n')
    return out
