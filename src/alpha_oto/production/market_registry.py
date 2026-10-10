"""Versioned, explicit simulation market sessions and instrument whitelist.

Market-hour information is never guessed. Config is intentionally simulator-only.
For actual venues, holiday/calendar coverage and permitted order types require
provider-specific approval and verification. No automatic live authorization.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime, date, time, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .contracts import D, Instrument, OrderIntent, Quote, RiskRejected, utc


@dataclass(frozen=True)
class MarketSpec:
    symbol: str
    venue: str
    currency: str
    tick: str
    lot: str
    max_units: str
    timezone: str
    continuous: bool
    weekdays: tuple[int, ...] = ()
    session_start: str | None = None
    session_end: str | None = None
    holidays: tuple[str, ...] = ()
    valid_through: str | None = None
    simulation_approved: bool = True

    def __post_init__(self):
        Instrument(self.symbol,self.venue,self.currency,D(self.tick),D(self.lot),D(self.max_units),self.simulation_approved)
        try: ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc: raise ValueError('Unknown time zone') from exc
        if not isinstance(self.simulation_approved,bool): raise ValueError('Bad approval flag')
        if not isinstance(self.continuous,bool): raise ValueError('Bad continuous flag')
        if self.continuous:
            if self.weekdays or self.session_start or self.session_end or self.holidays or self.valid_through:
                raise ValueError('Continuous market must not define session rules')
        else:
            if not self.weekdays or any(not isinstance(d,int) or isinstance(d,bool) or not 0<=d<=6 for d in self.weekdays):
                raise ValueError('Explicit weekdays required')
            if len(set(self.weekdays))!=len(self.weekdays):raise ValueError('Duplicate weekdays')
            for s in (self.session_start,self.session_end):
                if not isinstance(s,str) or len(s)!=5 or s[2]!=':':raise ValueError('HH:MM session required')
                try: time.fromisoformat(s)
                except ValueError as exc: raise ValueError('Invalid session') from exc
            if self.session_start==self.session_end: raise ValueError('Zero-length session')
            if not self.valid_through:raise ValueError('Explicit calendar validity required')
            try:
                last=date.fromisoformat(self.valid_through)
                if any(date.fromisoformat(d)>last for d in self.holidays):raise ValueError('Holiday after calendar validity')
            except ValueError as exc:raise ValueError('Invalid calendar validity / holiday') from exc
            if len(set(self.holidays))!=len(self.holidays):raise ValueError('Duplicate holidays')

    def instrument(self) -> Instrument:
        return Instrument(self.symbol,self.venue,self.currency,D(self.tick),D(self.lot),D(self.max_units),self.simulation_approved)

    def is_open(self, at:datetime)->bool:
        instant=utc(at).astimezone(ZoneInfo(self.timezone))
        if self.continuous:return True
        local=instant.timetz().replace(tzinfo=None)
        opening=time.fromisoformat(self.session_start)
        closing=time.fromisoformat(self.session_end)
        if opening<closing:
            trading_date=instant.date()
            in_hours=opening<=local<closing
        else:
            in_hours=local>=opening or local<closing
            trading_date=instant.date() if local>=opening else instant.date()-timedelta(days=1)
        return (in_hours and trading_date.weekday() in self.weekdays
                and trading_date.isoformat() not in self.holidays
                and trading_date<=date.fromisoformat(self.valid_through))


class MarketRegistry:
    def __init__(self, markets:list[MarketSpec]|tuple[MarketSpec,...], *, version:str):
        if not version or not version.replace('-','').replace('_','').replace('.','').isalnum():
            raise ValueError('Version required')
        if not markets or len({m.symbol for m in markets})!=len(markets):raise ValueError('Unique symbols required')
        self.version=version
        self.markets={m.symbol:m for m in markets}
        self.digest=sha256(json.dumps({'version':version,'markets':[asdict(m) for m in sorted(markets,key=lambda m:m.symbol)]},
                             sort_keys=True,separators=(',',':')).encode()).hexdigest()

    @classmethod
    def from_file(cls,path):
        data=json.loads(Path(path).read_text())
        if set(data)!={'version','markets'}:raise ValueError('Unexpected registry keys')
        allowed=set(MarketSpec.__dataclass_fields__)
        if not isinstance(data['markets'],list):raise ValueError('Expected market array')
        markets=[]
        for row in data['markets']:
            if not isinstance(row,dict) or set(row)-allowed:
                raise ValueError('Unsupported registry field')
            normalized=dict(row)
            normalized['weekdays']=tuple(row.get('weekdays',()))
            normalized['holidays']=tuple(row.get('holidays',()))
            markets.append(MarketSpec(**normalized))
        return cls(markets,version=data['version'])


    def authorize(self,intent:OrderIntent,quote:Quote,now:datetime):
        entry=self.markets.get(intent.symbol)
        if entry is None or not entry.simulation_approved:
            raise RiskRejected('Instrument not whitelisted in simulation registry')
        if intent.symbol!=quote.symbol:raise RiskRejected('Quote/order instrument mismatch')
        if not entry.is_open(now):raise RiskRejected('Venue is closed or holiday/calendar unverified')
        if intent.quantity%entry.instrument().lot or intent.limit%entry.instrument().tick:
            raise RiskRejected('Invalid instrument tick or lot')
        return entry
