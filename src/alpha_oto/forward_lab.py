"""Forward-only, PRECOMMITTED paper research. NO orders, no broker, no promises.

One explicit invocation creates predictions for a future bar (never already-open
bars); a later invocation *observes* fills at the next bar's OHLC open. A
forecast must be committed before that open with a lead-time margin. Historical
bars can initialize features, but cannot be scored as new outcomes at freeze.
All fill prices are modeled OHLC proxies, NOT executable bid/ask quotations.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from math import isfinite, floor
from pathlib import Path
from typing import Sequence
import json
import os
import tempfile
import fcntl

from .data import Bar
from .portfolio import load_universe, timeline_for, PortfolioConfig
from .quant_agents import QuantAgent, allocate_scores, realized_vol

UTC = timezone.utc
VERSION = 'alpha_oto_forward_v1'
DEFAULT_STRATEGIES = (("breakout", 24, 120), ("trend", 48, 168))


def _canonical(data) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _digest(data) -> str:
    return sha256(_canonical(data)).hexdigest()


def _bar_dict(b: Bar) -> dict:
    return {'timestamp':b.timestamp.astimezone(UTC).isoformat(), 'symbol':b.symbol,
            'open':b.open,'high':b.high,'low':b.low,'close':b.close,'volume':b.volume}


def _prefix_hash(bars: Sequence[Bar], until: datetime) -> str:
    return _digest([_bar_dict(b) for b in bars if b.timestamp <= until])


def _parse(s: str) -> datetime:
    t = datetime.fromisoformat(s)
    if t.tzinfo is None:
        raise ValueError('Naive state timestamp')
    return t.astimezone(UTC)


def _now(now):
    if now is None:
        now = datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError('Clock must be timezone-aware')
    return now.astimezone(UTC)


def _event(state:dict, kind:str, payload:dict):
    previous = state['events'][-1]['hash'] if state['events'] else '0'*64
    item={'seq':len(state['events']), 'previous':previous, 'kind':kind, 'payload':payload}
    item['hash']=_digest(item)
    state['events'].append(item)


def _verify(state:dict):
    if state.get('schema') != VERSION or state.get('safety') != 'RESEARCH_ONLY_NO_BROKER_ORDERS':
        raise ValueError('Unsupported or unsafe research state')
    prev='0'*64
    for i,e in enumerate(state['events']):
        if e.get('seq')!=i or e.get('previous')!=prev or e.get('hash') != _digest({k:v for k,v in e.items() if k!='hash'}):
            raise ValueError('Forward journal broken or tampered with')
        prev=e['hash']
    # Hash chain is not a signature; it detects accidental edits, not malicious re-signing.


def _write(path:Path,state):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=f'.{path.name}.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(state,f,indent=2,sort_keys=True,allow_nan=False)
            f.write('\n');f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def _load(path):
    state=json.loads(Path(path).read_text())
    _verify(state)
    return state


def _validate_inputs(files, state, *, now):
    data=load_universe(files)
    symbols=sorted(data)
    if symbols!=state['symbols']:
        raise ValueError('Instrument universe changed after freeze')
    if len({s.rsplit('-',1)[-1] for s in symbols})!=1 or not all(s.endswith('-USD') for s in symbols):
        raise ValueError('V1 forward research only supports common USD quoted 24/7 spot series')
    interval=state['interval_seconds']
    for s in symbols:
        anchor=_parse(state['anchors'][s]['last_candle'])
        if _prefix_hash(data[s],anchor) != state['anchors'][s]['history_digest']:
            raise ValueError(f'Frozen history was changed or deleted: {s}')
        if data[s][-1].timestamp+timedelta(seconds=interval)>now:
            raise ValueError('Latest dataset contains an unfinished candle')
        observed=state['observed_candle_hashes'][s]
        lookup={b.timestamp.isoformat():_digest(_bar_dict(b)) for b in data[s]}
        for t,h in observed.items():
            if lookup.get(t)!=h:
                raise ValueError(f'Previously observed forward candle changed or vanished: {s} at {t}')
    return data


def _fresh_history(bars:list[Bar], before:datetime, interval:int, needed:int) -> list[Bar]:
    hist=[b for b in bars if b.timestamp < before and b.timestamp+timedelta(seconds=interval)<=before]
    if len(hist)<needed: return []
    hist=hist[-needed:]
    step=timedelta(seconds=interval)
    if any(b.timestamp-a.timestamp!=step for a,b in zip(hist,hist[1:])):
        return []
    return hist


def _nav(book, prices):
    return book['cash']+sum(book['positions'][s]*prices[s] for s in prices)


def freeze_forward(files:list[str], out:str, *, now=None, interval_seconds:int=3600,
                   starting_cash:float=100_000., config:PortfolioConfig|None=None,
                   strategies=DEFAULT_STRATEGIES):
    """Freeze everything before wall-clock time. Existing historical data = context only."""
    now=_now(now)
    if interval_seconds<60 or starting_cash<=0 or not isfinite(starting_cash):
        raise ValueError('Invalid clock interval or balance')
    data=load_universe(files)
    symbols=sorted(data)
    if len(symbols)<2 or len({s.rsplit('-',1)[-1] for s in symbols})!=1 or not all(s.endswith('-USD') for s in symbols):
        raise ValueError('Only 2+ common USD-quoted 24/7 spot assets are supported')
    if config is None: config=PortfolioConfig(initial_cash=starting_cash,interval_seconds=interval_seconds,
                                             max_gross=.60,max_asset=.30,max_drawdown=.15,
                                             regime_control=False)
    if config.initial_cash!=starting_cash or config.interval_seconds!=interval_seconds:
        raise ValueError('Conflicting paper risk configuration')
    anchors={}
    for sym,bars in data.items():
        if bars[-1].timestamp+timedelta(seconds=interval_seconds)>now:
            raise ValueError('Cannot freeze a dataset containing an unfinished bar')
        if now-bars[-1].timestamp>timedelta(seconds=3*interval_seconds):
            raise ValueError('Dataset is stale; update recent candles before freeze')
        anchors[sym]={'last_candle':bars[-1].timestamp.isoformat(),
                      'history_digest':_prefix_hash(bars,bars[-1].timestamp)}
    specs=[]
    for kind,fast,slow in strategies:
        a=QuantAgent(kind,fast,slow)
        if a.name in [x['name'] for x in specs]: raise ValueError('Duplicate strategies')
        specs.append({'name':a.name,'kind':kind,'fast':fast,'slow':slow})
    names=['equal_weight_benchmark','cash_benchmark']+[x['name'] for x in specs]
    books={name:{'cash':starting_cash,'positions':{s:0.0 for s in symbols},
                 'fees':0.,'fills':0,'peak':starting_cash,'max_drawdown':0.,
                 'halted':False,'last_mark':starting_cash} for name in names}
    state={'schema':VERSION,'safety':'RESEARCH_ONLY_NO_BROKER_ORDERS',
           'created_at_utc':now.isoformat(),'interval_seconds':interval_seconds,
           'symbols':symbols,'risk':{'max_gross':config.max_gross,'max_asset':config.max_asset,
               'max_drawdown':config.max_drawdown,'side_fee_bps':config.side_fee_bps,
               'side_slippage_bps':config.side_slippage_bps,
               'max_participation':config.max_participation,
               'min_trade_notional':config.min_trade_notional},
           'starting_cash':starting_cash,'specs':specs,'books':books,'anchors':anchors,
           'observed_candle_hashes':{s:{} for s in symbols},
           'last_prices':{},'pending_intent':None,'events':[],
           'complete_forward_bars':0,'resolved_intents':0,'missed_intents':0}
    _event(state,'FROZEN',{'created_at':now.isoformat(),'symbols':symbols,
                           'strategy_names':names,'research_only':True})
    path=Path(out)
    if path.exists():raise FileExistsError('Refusing to overwrite a frozen research experiment')
    _write(path,state)
    return {'file':str(path),'frozen_at':now.isoformat(),
            'strategy_names':names,'status':state['safety'],
            'note':'Run forward-step after new completed candles arrive. Predictions must predate their target bar.'}


def _weights(spec,state,data, target, interval):
    symbols=state['symbols'];risk=state['risk']
    if spec=='cash_benchmark': return {s:0. for s in symbols},{}
    if spec=='equal_weight_benchmark':
        w=min(risk['max_asset'],risk['max_gross']/len(symbols))
        return {s:w for s in symbols},{}
    args=next(x for x in state['specs'] if x['name']==spec)
    agent=QuantAgent(args['kind'],args['fast'],args['slow'])
    scores={};vol={};prior_volume={}
    # To guarantee precommit before target OPEN, latest usable candle must have
    # finished before decision time (usually target-2*interval, not target-1).
    for s in symbols:
        hist=_fresh_history(data[s],target,interval,agent.slow+1)
        hist=[b for b in hist if b.timestamp+timedelta(seconds=interval)<=_parse(state['current_clock'])]
        if len(hist)<agent.slow+1:
            return {s:0. for s in symbols},{}
        scores[s]=agent.score(hist)
        vol[s]=realized_vol(hist,min(agent.lookback,48))
        prior_volume[s]=hist[-1].volume
    w=allocate_scores(scores,vol,gross_limit=risk['max_gross'],
                      per_asset_limit=risk['max_asset'],volatility_target=.012)
    return w,prior_volume


def _paper_fill(state,intent,bars_at):
    """OHLC-based hypothetical fill, no brokerage. Prior volume is committed."""
    risk=state['risk'];symbols=state['symbols'];time=intent['execute_at']
    next_open={s:bars_at[s].open for s in symbols}
    for name,book in state['books'].items():
        nav_before=_nav(book,next_open)
        target=intent['proposals'][name]['weights']
        # A drawdown halt occurring AFTER intent precommitment is binding
        # at simulated execution: never open a new long while halted.
        if book['halted']:
            target={sym:0.0 for sym in symbols}
        for side in ('SELL','BUY'):
            for sym in symbols:
                slip=risk['side_slippage_bps']/1e4
                fee_rate=risk['side_fee_bps']/1e4
                px=next_open[sym]*(1+slip if side=='BUY' else 1-slip)
                diff=(target[sym]*nav_before -book['positions'][sym]*next_open[sym]) if side=='BUY' else (book['positions'][sym]*next_open[sym]-target[sym]*nav_before)
                if diff < risk['min_trade_notional']:continue
                capacity=intent['proposals'][name]['prior_volume'].get(sym,0)*risk['max_participation']*next_open[sym]
                notional=min(diff,capacity)
                if side=='BUY':notional=min(notional,book['cash']/(1+fee_rate))
                else:notional=min(notional,book['positions'][sym]*px)
                if notional <risk['min_trade_notional']:continue
                qty=notional/px
                if side=='BUY':
                    book['cash']-=notional*(1+fee_rate)
                    book['positions'][sym]+=qty
                else:
                    book['cash']+=notional*(1-fee_rate)
                    book['positions'][sym]=max(0.,book['positions'][sym]-qty)
                book['fees']+=notional*fee_rate;book['fills']+=1
                _event(state,'PAPER_FILL',{'strategy':name,'symbol':sym,'time':time,'side':side,
                             'quantity':qty,'hypothetical_price':px,'fee':notional*fee_rate,
                             'OHLC_PROXY_NOT_EXECUTABLE':True})
        if book['cash'] < -1e-5: raise ValueError('Negative cash in paper ledger')


def _next_target(now, interval, min_lead_seconds=15):
    t=(floor(now.timestamp()/interval)+1)*interval
    if t-now.timestamp()<min_lead_seconds:t+=interval
    return datetime.fromtimestamp(t,UTC)


def advance_forward(files:list[str], state_path:str, *, now=None):
    """Observe already precommitted intents then schedule ONE future intent.

    Calls are serialized with fcntl; no trading code is imported anywhere.
    No automatic 'catch up' fake fills for periods with no precommitted intent.
    """
    now=_now(now);path=Path(state_path)
    if not path.is_file():raise FileNotFoundError('Freeze experiment first')
    with Path(str(path)+'.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        state=_load(path)
        if now < _parse(state['created_at_utc']):raise ValueError('Clock earlier than freeze')
        interval=state['interval_seconds'];dt=timedelta(seconds=interval)
        data=_validate_inputs(files,state,now=now)
        state['current_clock']=now.isoformat()
        lookups={s:{b.timestamp:b for b in bs} for s,bs in data.items()}
        timestamps=sorted({b.timestamp for bs in data.values() for b in bs
                           if b.timestamp>=_parse(state['created_at_utc']) and b.timestamp+dt<=now})
        updates=0;filled=False
        # Important: a previously committed target is processed only if it
        # was saved before the target opened, regardless of when we catch up.
        intent=state['pending_intent']
        for t in timestamps:
            iso=t.isoformat()
            if all(iso in state['observed_candle_hashes'][s] for s in state['symbols'] if t in lookups[s]):
                continue
            row={s:lookups[s].get(t) for s in state['symbols']}
            if intent and t==_parse(intent['execute_at']):
                if _parse(intent['committed_at'])+timedelta(seconds=15)>t:
                    raise ValueError('Intent was not committed before its execution time')
                if all(row.values()):
                    _paper_fill(state,intent,row);filled=True
                    state['resolved_intents']+=1
                else:
                    state['missed_intents']+=1
                    _event(state,'PAPER_FILL_SKIPPED',{'time':iso,'reason':'MISSING_QUOTE'})
                state['pending_intent']=None
                intent=None
            for s,b in row.items():
                if b is None: continue
                if iso not in state['observed_candle_hashes'][s]:
                    state['observed_candle_hashes'][s][iso]=_digest(_bar_dict(b))
            # Close-mark a book ONLY if every market quote exists at this time.
            if all(row.values()):
                state['complete_forward_bars']+=1
                marks={s:row[s].close for s in state['symbols']}
                for name,book in state['books'].items():
                    nav=_nav(book,marks)
                    if nav<=0 or not isfinite(nav):raise ValueError('Invalid NAV')
                    book['last_mark']=nav;book['peak']=max(book['peak'],nav)
                    book['max_drawdown']=max(book['max_drawdown'],1-nav/book['peak'])
                    if book['max_drawdown']>=state['risk']['max_drawdown']:
                        book['halted']=True
                state['last_prices']=marks
            else:
                _event(state,'STALE_MARK',{'time':iso,'missing':[s for s in row if row[s] is None]})
            updates+=1
        # If one target bar is missing from ALL files, the timestamp may not
        # occur in the union. Once it is in the past, retire without a fill.
        intent=state['pending_intent']
        if intent and _parse(intent['execute_at'])+dt<=now:
            state['missed_intents']+=1
            _event(state,'PAPER_FILL_SKIPPED',{'time':intent['execute_at'],'reason':'TARGET_BAR_NOT_AVAILABLE'})
            state['pending_intent']=None
        target=_next_target(now,interval)
        proposed=False
        if state['pending_intent'] is None:
            # All source files need a sufficiently recent completed candle,
            # to prevent a lost internet feed from generating stale signals.
            if all(any(b.timestamp>=target-2*dt and b.timestamp+dt<=now
                       for b in data[s]) for s in state['symbols']):
                proposals={}
                for name,book in state['books'].items():
                    w,vol=_weights(name,state,data,target,interval)
                    if book['halted']:w={s:0. for s in state['symbols']}
                    # Even passive baseline allocations need prior known depth
                    # proxy; never use the target candle's future volume.
                    recent={s:[b for b in data[s] if b.timestamp+dt<=now and b.timestamp<target] for s in state['symbols']}
                    prior_vol={s:recent[s][-1].volume if recent[s] else 0 for s in state['symbols']}
                    proposals[name]={'weights':w,'prior_volume':prior_vol,
                        'basis_latest_completed':{s:recent[s][-1].timestamp.isoformat() for s in state['symbols']}}
                state['pending_intent']={'execute_at':target.isoformat(),
                   'committed_at':now.isoformat(),'proposals':proposals}
                _event(state,'PAPER_INTENT_PRECOMMITTED',{'execute_at':target.isoformat(),
                    'committed_at':now.isoformat(),'weights':{k:v['weights'] for k,v in proposals.items()}})
                proposed=True
            else:
                _event(state,'NO_INTENT_STALE_DATA',{'now':now.isoformat(),'next_open':target.isoformat()})
        state.pop('current_clock',None)
        _write(path,state)
        return status_forward(state,observed_new=updates,filled=filled,new_intent=proposed)


def status_forward(state_or_path,*,observed_new=0,filled=False,new_intent=False):
    state=_load(state_or_path) if isinstance(state_or_path,(str,Path)) else state_or_path
    _verify(state)
    reference=state['books']['equal_weight_benchmark']['last_mark'] / state['starting_cash']-1
    evidence='INSUFFICIENT_NEW_FORWARD_OBSERVATIONS' if state['complete_forward_bars']<24*30 else 'DIAGNOSTICS_ONLY_REQUIRES_INDEPENDENT_REVIEW'
    return {'status':state['safety'],'frozen_at':state['created_at_utc'],
       'evidence_state':evidence,'complete_forward_bars':state['complete_forward_bars'],
       'resolved_precommitted_intents':state['resolved_intents'],
       'skipped_or_missing_intents':state['missed_intents'],
       'new_completed_market_times':observed_new,'paper_fill_observed':filled,
       'new_future_intent':new_intent,
       'pending_future_intent':state['pending_intent']['execute_at'] if state['pending_intent'] else None,
       'events':len(state['events']),
       'excess_return_vs_equal_weight':{name:round(book['last_mark']/state['starting_cash']-1-reference,8)
          for name,book in state['books'].items() if name!='equal_weight_benchmark'},
       'strategies':{name:{'hypothetical_nav':round(book['last_mark'],6),
                          'hypothetical_return':round(book['last_mark']/state['starting_cash']-1,8),
                          'paper_fills':book['fills'],'halted':book['halted'],
                          'max_close_drawdown':round(book['max_drawdown'],8)}
                     for name,book in state['books'].items()},
       'warning':'Forward-only OHLC paper estimates, never broker fills or evidence of executable alpha.'}
