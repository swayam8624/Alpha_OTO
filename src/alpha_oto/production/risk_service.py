"""Independent *simulated* order authorization through a Unix-domain socket.

The risk process, not an agent payload, reads the authoritative quote feed and
financial ledger. Authorization means an intent was durably inserted in the
simulated account; it is NOT a broker order or permission to operate live.

Important: this is process isolation, not a hardened security boundary against
host compromise or same-UID arbitrary code. No live broker connector exists.
"""
from __future__ import annotations
from dataclasses import asdict
from datetime import datetime,timezone
from decimal import Decimal
from pathlib import Path
import json
import os
import socket
import socketserver
from .contracts import D, IntegrityFailure, OrderIntent, RiskRejected, RiskLimits, utc
from .market_registry import MarketRegistry
from .market_feed import DurableQuoteFeed
from .store import LedgerStore, canonical

MAX_RPC_BYTES=16384


class RiskUnavailable(ConnectionError):pass


def parse_intent(raw:dict)->OrderIntent:
    allowed={'client_id','symbol','side','quantity','limit','created_at','strategy','model_version'}
    if not isinstance(raw,dict) or set(raw)!=allowed:
        raise ValueError('Unexpected order-intent fields')
    return OrderIntent(raw['client_id'],raw['symbol'],raw['side'],D(raw['quantity']),D(raw['limit']),
                       datetime.fromisoformat(raw['created_at']),raw['strategy'],raw['model_version'])


def intent_payload(intent:OrderIntent)->dict:
    return {'client_id':intent.client_id,'symbol':intent.symbol,'side':intent.side,
            'quantity':str(intent.quantity),'limit':str(intent.limit),
            'created_at':utc(intent.created_at).isoformat(),
            'strategy':intent.strategy,'model_version':intent.model_version}


class IsolatedRiskAuthority:
    """Only simulated account + feed; broker/emulator credentials never loaded."""
    def __init__(self,ledger_path,feed_path,registry:MarketRegistry,
                 *, initial_cash='10000',currency='USD',limits:RiskLimits=RiskLimits()):
        self.registry=registry
        self.feed=DurableQuoteFeed(feed_path)
        try:
            self.store=LedgerStore(ledger_path,initial_cash=D(initial_cash),currency=currency,
                                   instruments=tuple(spec.instrument() for spec in registry.markets.values()),
                                   limits=limits)
            self._pin_registry()
        except BaseException:
            if hasattr(self,'store'):
                self.store.close()
            self.feed.close()
            raise

    def _pin_registry(self):
        self.store.verify()
        with self.store.transaction():
            prior=self.store.db.execute('SELECT value FROM meta WHERE key=?',('risk_service_registry',)).fetchone()
            if prior is not None and prior['value']!=self.registry.digest:
                raise IntegrityFailure('Registry/calendar changed from previously pinned account')
            if prior is None:
                self.store.db.execute('INSERT INTO meta(key,value) VALUES (?,?)',('risk_service_registry',self.registry.digest))
                self.store._event('MARKET_REGISTRY_FROZEN',None,
                                  {'version':self.registry.version,'digest':self.registry.digest})

    def close(self):
        self.store.close();self.feed.close()

    def authorize(self,intent:OrderIntent,*,now:datetime|None=None):
        now=utc(now or datetime.now(timezone.utc))
        self.store.verify()
        q=self.feed.latest(intent.symbol)
        self.registry.authorize(intent,q,now)
        # The trading agent cannot provide or backdate the quote; this is the
        # timestamped authoritative quote from the independent feed store.
        self.store.record_mark(q,now)
        row=self.store.create_intent(intent,q,now)
        return {'result':'DURABLE_SIMULATION_INTENT_ONLY','client_id':row['client_id'],
                'state':row['state'],'registry_digest':self.registry.digest,
                'quote_source_time':utc(q.source_time).isoformat(),'live_approved':False}

    def halt(self):
        self.store.set_halt('External operator emergency halt via local Unix socket')
        return {'halted':True,'live_approved':False}

    def health(self):
        ledger=self.store.verify()
        feed=self.feed.status()
        now=datetime.now(timezone.utc)
        status='SIMULATION_RISK_SERVICE_OK'
        if self.store.summary()['halted']:
            status='HALTED'
        elif not feed['quotes']:
            status='NO_QUOTE_FEED'
        elif any(q['quarantined'] for q in feed['quotes']):
            status='QUARANTINED_FEED'
        elif any(not 0 <= (now-datetime.fromisoformat(q['source_time'])).total_seconds()
                         <= self.store.limits.max_quote_age_seconds for q in feed['quotes']):
            status='STALE_FEED'
        return {'status':status,'ledger':ledger,'feed':feed,
                'registry_digest':self.registry.digest,'mode':'SIMULATOR_ONLY',
                'safe_for_new_simulated_intents':status=='SIMULATION_RISK_SERVICE_OK',
                'live_approved':False}

    def handle(self,packet:dict):
        if not isinstance(packet,dict) or set(packet)-{'op','intent'}:
            raise ValueError('Unknown RPC fields')
        if packet.get('op')=='HEALTH' and set(packet)=={'op'}:
            return self.health()
        if packet.get('op')=='HALT' and set(packet)=={'op'}:
            return self.halt()
        if packet.get('op')=='AUTHORIZE' and set(packet)=={'op','intent'}:
            return self.authorize(parse_intent(packet['intent']))
        raise ValueError('Unknown risk operation')


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line=self.rfile.readline(MAX_RPC_BYTES+1)
        if not line or len(line)>MAX_RPC_BYTES or not line.endswith(b'\n'):
            self.wfile.write(b'{"ok":false,"error":"Invalid request size or framing"}\n');return
        try:
            request=json.loads(line.decode('utf-8'))
            response={'ok':True,'result':self.server.authority.handle(request)}
        except (ValueError,TypeError,KeyError,IntegrityFailure,RuntimeError) as exc:
            response={'ok':False,'error':type(exc).__name__,'reason':str(exc)[:300]}
        except BaseException:
            # Unexpected server failure: do not reveal credentials or internal
            # state. No authorization has been returned to the caller.
            response={'ok':False,'error':'InternalRiskError'}
        self.wfile.write((canonical(response)+'\n').encode())


class _RiskServer(socketserver.UnixStreamServer):
    allow_reuse_address=False
    def __init__(self,sock,authority):
        self.authority=authority
        super().__init__(str(sock),_Handler)


def open_server(sock_path,authority:IsolatedRiskAuthority):
    path=Path(sock_path)
    path.parent.mkdir(parents=True,exist_ok=True)
    if len(os.fsencode(str(path))) >= 99:
        raise ValueError('Unix socket path too long; use a short temporary path')
    if path.exists():
        # Existing socket may be an active server, or maliciously placed file.
        # Fail closed rather than unlinking a potentially live socket.
        raise IntegrityFailure('Socket path already exists; investigate rather than unlink automatically')
    old_umask=os.umask(0o177)
    try: server=_RiskServer(path,authority)
    finally: os.umask(old_umask)
    os.chmod(path,0o600)
    return server


class RiskClient:
    def __init__(self,sock_path,timeout=2.0):
        if timeout<=0:raise ValueError('Positive timeout required')
        self.sock_path=str(sock_path);self.timeout=timeout

    def call(self,op,*,intent:OrderIntent|None=None):
        request={'op':op}
        if intent is not None:request['intent']=intent_payload(intent)
        message=(canonical(request)+'\n').encode()
        if len(message)>MAX_RPC_BYTES:raise RiskUnavailable('Request exceeds allowed size')
        try:
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                s.connect(self.sock_path)
                s.sendall(message)
                buffer=b''
                while not buffer.endswith(b'\n'):
                    chunk=s.recv(4096)
                    if not chunk:raise RiskUnavailable('Risk process closed connection')
                    buffer+=chunk
                    if len(buffer)>MAX_RPC_BYTES:raise RiskUnavailable('Oversized risk response')
        except (OSError,TimeoutError) as exc:
            raise RiskUnavailable('Risk authority unavailable; must not submit') from exc
        try: reply=json.loads(buffer)
        except (ValueError,UnicodeError) as exc:raise RiskUnavailable('Corrupt risk reply') from exc
        if reply.get('ok') is not True:
            raise RiskRejected(f"Risk authority declined {reply.get('error')}: {reply.get('reason')}")
        return reply['result']

    def authorize(self,intent:OrderIntent):
        return self.call('AUTHORIZE',intent=intent)

    def health(self):return self.call('HEALTH')

    def halt(self):return self.call('HALT')


class SeparatedSimulationGateway:
    """Agent-side adapter. All new intents pass through the separate risk daemon.

    Still a SIMULATION: cannot establish privilege separation for a live broker
    until an order-routing process with exclusive credentials is developed.
    """
    def __init__(self,risk_client:RiskClient,simulation_gateway):
        self.risk=risk_client
        self.gateway=simulation_gateway

    def propose(self,intent:OrderIntent):
        result=self.risk.authorize(intent)
        record=self.gateway.store.order(intent.client_id)
        if not record or record['state'] not in ('CREATED','SUBMITTING','ACKNOWLEDGED','PARTIAL','FILLED'):
            raise IntegrityFailure('Risk daemon returned success but authorized order not durable')
        return result

    def dispatch(self,intent:OrderIntent):
        # This step is fake broker dispatch only. A future live order router
        # MUST independently enforce risk authorization and broker controls.
        return self.gateway.dispatch(intent)
