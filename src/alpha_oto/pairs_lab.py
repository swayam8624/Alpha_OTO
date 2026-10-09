"""Long/short relative-value statistical-arbitrage *research*, no broker.

The simulator expresses a hypothetical dollar-gross-neutral two-leg exposure,
NOT a cash equity strategy or executable retail short. Borrow availability,
funding, venue permissions and broker margin are not validated. Never use as
an execution signal. Hedge regression trained before evaluation period only.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
from math import log, sqrt, isfinite
from pathlib import Path
from statistics import mean, pstdev
import json
from .data import read_csv, Bar
from .portfolio import PortfolioConfig, load_universe, timeline_for


@dataclass(frozen=True)
class Hedge:
    alpha: float
    beta: float
    fit_count: int
    train_residual_sd: float
    estimated_half_life: float | None
    adf_pvalue: float | None
    cointegration_verified: bool


def fit_hedge(x:list[float], y:list[float]) -> Hedge:
    """OLS log(y) = alpha + beta*log(x) on TRAINING rows only.

    ADF diagnostic is optional; in its absence NO stationarity claim is made.
    Engle-Granger critical values differ from standard ADF; an optional raw
    residual ADF p-value is diagnostic only, not a valid cointegration test.
    """
    if len(x)!=len(y) or len(x)<100 or min(x+y)<=0:
        raise ValueError('Require >=100 positive synchronized close pairs')
    a=[log(v) for v in x]; b=[log(v) for v in y]
    ma=mean(a);mb=mean(b)
    var=sum((z-ma)**2 for z in a)
    if var<1e-10:
        raise ValueError('Regressor prices are nearly constant')
    beta=sum((v-ma)*(w-mb) for v,w in zip(a,b))/var
    alpha=mb-beta*ma
    residual=[w-alpha-beta*v for v,w in zip(a,b)]
    if pstdev(residual)<1e-10:
        raise ValueError('Degenerate spread regression')
    lag=residual[:-1]; d=[residual[i]-residual[i-1] for i in range(1,len(residual))]
    den=sum((v-mean(lag))**2 for v in lag)
    slope=sum((v-mean(lag))*(dv-mean(d)) for v,dv in zip(lag,d))/den if den>1e-12 else 0
    halflife=(-log(2)/log(1+slope) if -1<slope<0 else None)
    # No ADF/cointegration inference. Correct critical-value analysis needs a
    # deliberate separate methodology and appropriate paired sample selection.
    return Hedge(alpha,beta,len(x),pstdev(residual),halflife,None,False)


def spread_zscores(prices_x:list[float],prices_y:list[float],hedge:Hedge,lookback:int=72):
    if len(prices_x)!=len(prices_y) or lookback<20:
        raise ValueError('Invalid spread observations')
    residual=[log(y)-hedge.alpha-hedge.beta*log(x) for x,y in zip(prices_x,prices_y)]
    zs=[]
    for t in range(len(residual)):
        if t<lookback:
            zs.append(None);continue
        # Estimate mean/scale only from earlier historical observations.
        prior=residual[t-lookback:t]
        sigma=pstdev(prior)
        zs.append((residual[t]-mean(prior))/sigma if sigma>1e-10 else None)
    return zs


def simulate_pair(xbars:list[Bar],ybars:list[Bar],hedge:Hedge,
                  *, start:int,stop:int,entry_z:float=2.,exit_z:float=.5,
                  lookback:int=72,initial_cash:float=100_000.,
                  gross_fraction:float=.20,side_cost_bps:float=25.,
                  borrow_annual:float=.08,interval_seconds:int=3600) -> dict:
    if not 0<exit_z<entry_z or not 0<gross_fraction<=1 or initial_cash<=0:
        raise ValueError('Invalid pair execution parameters')
    if min(side_cost_bps,borrow_annual)<0:
        raise ValueError('Invalid financial costs')
    if len(xbars)!=len(ybars) or not lookback+1<=start<stop<=len(xbars):
        raise ValueError('Incompatible pairs research interval')
    if any(a.timestamp!=b.timestamp for a,b in zip(xbars,ybars)):
        raise ValueError('Pair timestamps not synchronized')
    z=spread_zscores([b.close for b in xbars],[b.close for b in ybars],hedge,lookback)
    equity=initial_cash
    peak=equity;maxdd=0.;turnover=0.;trades=0;blocked=0
    qx=qy=0.
    marks=None
    action_log=[];curve=[]
    elapsed_bars=1
    for t in range(start,stop):
        a,b=xbars[t],ybars[t]
        continuous=(xbars[t-1].timestamp+__import__('datetime').timedelta(seconds=interval_seconds)==a.timestamp
                    and ybars[t-1].timestamp+__import__('datetime').timedelta(seconds=interval_seconds)==b.timestamp)
        # Existing position is marked from prior close to current open even if
        # the time gap contains unobservable price movements. No fresh entry
        # or exit can be authorized at a missing-candle boundary.
        if marks:
            equity+=qx*(a.open-marks[0])+qy*(b.open-marks[1])
        prior=z[t-1]
        desire=0
        if prior is not None and continuous:
            if qx==qy==0:
                if prior>entry_z: desire=-1   # short expensive Y, long X
                elif prior < -entry_z: desire=1  # long cheap Y, short X
            elif abs(prior)<exit_z:
                desire=0
            else:
                desire=1 if qy>0 else -1
        elif not continuous:
            blocked+=1
            desire=1 if qy>0 else -1 if qy<0 else 0
        else:
            desire=1 if qy>0 else -1 if qy<0 else 0
        current=1 if qy>0 else -1 if qy<0 else 0
        if desire!=current and continuous:
            if current:
                traded=abs(qx*a.open)+abs(qy*b.open)
                equity-=traded*side_cost_bps/10000
                turnover+=traded
                action_log.append({'timestamp':a.timestamp.isoformat(),'action':'EXIT',
                                   'gross_notional':traded})
                qx=qy=0.
                trades+=1
            if desire and equity>0:
                # Gross-notional neutrality for simple research. Hedge beta
                # enters SPREAD SIGNAL but not an executable beta hedge.
                leg=equity*gross_fraction/2
                qy=desire*leg/b.open
                qx=-desire*leg/a.open
                equity-=2*leg*side_cost_bps/10000
                turnover+=2*leg
                action_log.append({'timestamp':a.timestamp.isoformat(),'action':'ENTER',
                                   'direction':desire,'gross_notional':2*leg})
        equity+=qx*(a.close-a.open)+qy*(b.close-b.open)
        short_notional=abs(qx)*a.close if qx<0 else abs(qy)*b.close if qy<0 else 0
        equity-=short_notional*borrow_annual*(interval_seconds/(365*24*3600))
        peak=max(peak,equity)
        maxdd=max(maxdd,1-equity/peak)
        marks=(a.close,b.close)
        if equity<=0:
            raise ValueError('Research pair strategy became insolvent')
        curve.append({'timestamp':a.timestamp.isoformat(),'equity':equity})
    # Reporting-only closeout estimate includes both legs and friction.
    if qx or qy:
        a=xbars[stop-1];b=ybars[stop-1]
        equity-=(abs(qx*a.close)+abs(qy*b.close))*side_cost_bps/10000
    return {'net_return':equity/initial_cash-1,'ending_equity':equity,
            'max_drawdown':maxdd,'completed_trades':trades,
            'trade_actions':len(action_log),'blocked_gap_boundaries':blocked,
            'turnover_fraction':turnover/initial_cash,
            'trade_log':action_log,'equity_points':len(curve),
            'warning':'Hypothetical unavailable short legs/margin, no actual bid-ask or borrowing approval.'}


def pairs_research(files:list[str|Path],output:str|Path,
                   *,interval_seconds:int=3600,lookback:int=72,
                   thresholds:tuple[float,...]=(1.5,2.,2.5),
                   longest_contiguous_segment:bool=False) -> dict:
    if len(files)!=2:
        raise ValueError('Pairs lab needs exactly two synchronized assets')
    u=load_universe(files)
    cfg=PortfolioConfig(interval_seconds=interval_seconds)
    times=timeline_for(u,cfg)
    assets=sorted(u)
    maps={s:{b.timestamp:b for b in u[s]} for s in assets}
    full_count=len(times)
    unavailable=[t for t in times if any(t not in maps[s] for s in assets)]
    chosen_segment={'total_hours':full_count,'missing_hours':len(unavailable),
                    'segment_strategy':'FULL_CONTIGUOUS_ONLY'}
    if unavailable and not longest_contiguous_segment:
        raise ValueError('Pairs lab refuses candle gaps; specify --longest-contiguous-segment')
    if unavailable:
        runs=[]; start=None
        for i,t in enumerate(times):
            valid=all(t in maps[s] for s in assets)
            if valid and start is None: start=i
            elif not valid and start is not None:
                runs.append((start,i));start=None
        if start is not None:runs.append((start,len(times)))
        a,b=max(runs,key=lambda x:x[1]-x[0])
        times=times[a:b]
        chosen_segment.update({'segment_strategy':'LONGEST_VERIFIED_CONTIGUOUS_ONLY',
                               'segment_start_utc':times[0].isoformat(),
                               'segment_end_utc':times[-1].isoformat(),
                               'used_hours':len(times),'excluded_hours':full_count-len(times)})
    xs=[maps[assets[0]][t] for t in times]
    ys=[maps[assets[1]][t] for t in times]
    if len(xs)<800:
        raise ValueError('Insufficient paired candles')
    training=int(.5*len(xs)); validation=int(.8*len(xs))
    hedge=fit_hedge([b.close for b in xs[:training]],
                    [b.close for b in ys[:training]])
    scores=[]
    for threshold in thresholds:
        r=simulate_pair(xs,ys,hedge,start=training,stop=validation,
                        lookback=lookback,entry_z=threshold)
        scores.append({'entry_z':threshold,'validation':r})
    # Disallow confident 'tradable' labeling without proof of cointegration,
    # operational borrow permissions and a realized profitable validation set.
    selected=max(scores,key=lambda s:s['validation']['net_return'])
    result={'status':'NON_EXECUTABLE_RELATIVE_VALUE_RESEARCH',
            'assets':assets,'selected_segment':chosen_segment,'hedge':asdict(hedge),
            'candidate_validation':scores,
            'selected_hypothesis':selected['entry_z'],
            'holdout':'NOT_EVALUATED_NO_STATIONARITY_OR_SHORT_ACCESS_VERIFICATION',
            'warning':'Cannot call residual ADF or spread regression proof of cointegration; no executable short-leg evidence.'}
    path=Path(output)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    return result
