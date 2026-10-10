"""Durable, sequenced L1 quote feed for SIMULATION and offline replay only.

The feed refuses gaps, rewinds, revisions and stale snapshots. No synthetic
price interpolation, network subscriptions, trade placement or broker access.
"""
from __future__ import annotations
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import sqlite3
from .contracts import D, IntegrityFailure, Quote, RiskRejected, utc
from .store import canonical


def quote_payload(q:Quote):
    return {'symbol':q.symbol,'bid':str(q.bid),'ask':str(q.ask),
            'bid_size':str(q.bid_size),'ask_size':str(q.ask_size),
            'source_time':utc(q.source_time).isoformat(),'received_time':utc(q.received_time).isoformat()}


def parse_quote(payload:dict)->Quote:
    return Quote(payload['symbol'],D(payload['bid']),D(payload['ask']),
                 D(payload['bid_size']),D(payload['ask_size']),
                 datetime.fromisoformat(payload['source_time']),
                 datetime.fromisoformat(payload['received_time']))


class SequenceGap(IntegrityFailure):pass


class DurableQuoteFeed:
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path,isolation_level=None,timeout=5)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS quote_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,
             symbol TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL,
             prev_hash TEXT NOT NULL, hash TEXT NOT NULL, UNIQUE(symbol,seq));
          CREATE TABLE IF NOT EXISTS quote_latest(symbol TEXT PRIMARY KEY,seq INTEGER NOT NULL,
             payload TEXT NOT NULL,quarantined INTEGER NOT NULL DEFAULT 0,reason TEXT NOT NULL DEFAULT '');
          CREATE TABLE IF NOT EXISTS feed_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS quote_incidents(id INTEGER PRIMARY KEY AUTOINCREMENT,
             symbol TEXT NOT NULL,reason TEXT NOT NULL,prev_hash TEXT NOT NULL,hash TEXT NOT NULL);
        ''')
        self.verify()

    def close(self):self.db.close()

    def _append(self,symbol,seq,payload):
        last=self.db.execute('SELECT hash FROM quote_events ORDER BY event_id DESC LIMIT 1').fetchone()
        previous=last['hash'] if last else 'GENESIS'
        signed={'symbol':symbol,'seq':seq,'payload':payload,'prev_hash':previous}
        digest=sha256(canonical(signed).encode()).hexdigest()
        self.db.execute('INSERT INTO quote_events(symbol,seq,payload,prev_hash,hash) VALUES (?,?,?,?,?)',
                        (symbol,seq,canonical(payload),previous,digest))

    def _incident(self,symbol:str,reason:str):
        prior=self.db.execute('SELECT hash FROM quote_incidents ORDER BY id DESC LIMIT 1').fetchone()
        prev=prior['hash'] if prior else 'GENESIS'
        digest=sha256(canonical({'symbol':symbol,'reason':reason,'prev_hash':prev}).encode()).hexdigest()
        self.db.execute('INSERT INTO quote_incidents(symbol,reason,prev_hash,hash) VALUES (?,?,?,?)',
                        (symbol,reason,prev,digest))

    def verify(self):
        incidents={}
        last_incident_hash='GENESIS'
        for entry in self.db.execute('SELECT * FROM quote_incidents ORDER BY id'):
            expected=sha256(canonical({'symbol':entry['symbol'],'reason':entry['reason'],
                                       'prev_hash':last_incident_hash}).encode()).hexdigest()
            if entry['prev_hash']!=last_incident_hash or entry['hash']!=expected:
                raise IntegrityFailure('Quote incident log modified')
            last_incident_hash=expected
            incidents[entry['symbol']]=entry['reason']
        previous='GENESIS'
        last_by_symbol={}
        for row in self.db.execute('SELECT * FROM quote_events ORDER BY event_id'):
            record={'symbol':row['symbol'],'seq':row['seq'],
                    'payload':json.loads(row['payload']),'prev_hash':previous}
            digest=sha256(canonical(record).encode()).hexdigest()
            if row['prev_hash']!=previous or row['hash']!=digest:
                raise IntegrityFailure('Quote event hash chain corrupted')
            previous=digest
            last_by_symbol[row['symbol']]=(row['seq'],canonical(record['payload']))
        for r in self.db.execute('SELECT * FROM quote_latest'):
            if r['symbol'] not in last_by_symbol or (r['seq'],r['payload'])!=last_by_symbol[r['symbol']]:
                raise IntegrityFailure('Latest quote snapshot differs from event log')
            if bool(r['quarantined']) != (r['symbol'] in incidents) or r['reason'] != incidents.get(r['symbol'],''):
                raise IntegrityFailure('Feed quarantine state differs from incident history')
        if len(last_by_symbol)!=self.db.execute('SELECT COUNT(*) FROM quote_latest').fetchone()[0]:
            raise IntegrityFailure('Quote snapshot deleted')
        return {'events':sum(1 for _ in self.db.execute('SELECT event_id FROM quote_events')),
                'symbols':len(last_by_symbol),'verified':True,
                'quarantined':sum(r['quarantined'] for r in self.db.execute('SELECT quarantined FROM quote_latest'))}

    def publish(self,sequence:int,quote:Quote):
        if not isinstance(sequence,int) or isinstance(sequence,bool) or sequence<1:raise ValueError('Positive feed sequence required')
        symbol=quote.symbol
        payload=quote_payload(quote)
        encoded=canonical(payload)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            latest=self.db.execute('SELECT * FROM quote_latest WHERE symbol=?',(symbol,)).fetchone()
            if latest:
                if latest['quarantined']:
                    raise SequenceGap('Feed quarantined: '+latest['reason'])
                if sequence==latest['seq'] and encoded==latest['payload']:
                    self.db.execute('COMMIT');return {'status':'DUPLICATE_SAME_QUOTE','sequence':sequence}
                if sequence<=latest['seq'] or sequence!=latest['seq']+1:
                    reason='OUT_OF_ORDER_CONFLICT' if sequence<=latest['seq'] else 'SEQUENCE_GAP'
                    self._incident(symbol,reason)
                    self.db.execute('UPDATE quote_latest SET quarantined=1,reason=? WHERE symbol=?',(reason,symbol))
                    self.db.execute('COMMIT')
                    raise SequenceGap(reason)
                prior=parse_quote(json.loads(latest['payload']))
                if utc(quote.source_time)<utc(prior.source_time) or utc(quote.received_time)<utc(prior.received_time):
                    self._incident(symbol,'CLOCK_ROLLBACK')
                    self.db.execute('UPDATE quote_latest SET quarantined=1,reason=? WHERE symbol=?',('CLOCK_ROLLBACK',symbol))
                    self.db.execute('COMMIT')
                    raise SequenceGap('CLOCK_ROLLBACK')
            self._append(symbol,sequence,payload)
            self.db.execute('INSERT INTO quote_latest(symbol,seq,payload) VALUES (?,?,?) ON CONFLICT(symbol) DO UPDATE SET seq=excluded.seq,payload=excluded.payload',
                            (symbol,sequence,encoded))
            self.db.execute('COMMIT')
            return {'status':'QUOTE_STORED','sequence':sequence}
        except BaseException:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            raise

    def latest(self,symbol:str)->Quote:
        self.verify()
        row=self.db.execute('SELECT * FROM quote_latest WHERE symbol=?',(symbol,)).fetchone()
        if not row:raise RiskRejected('No feed quote for instrument')
        if row['quarantined']:raise RiskRejected('Feed quarantined: '+row['reason'])
        return parse_quote(json.loads(row['payload']))

    def status(self):
        self.verify()
        return {'mode':'READ_ONLY_SIMULATED_QUOTES','quotes':[
            {'symbol':r['symbol'],'sequence':r['seq'],'quarantined':bool(r['quarantined']),
             'reason':r['reason'],'source_time':json.loads(r['payload'])['source_time']}
            for r in self.db.execute('SELECT * FROM quote_latest ORDER BY symbol')],
            'verified':True}


def replay_jsonl(path:str,feed:DurableQuoteFeed):
    """Offline trace: each row is {'sequence': N, 'quote': {...}}. Never live."""
    count=0
    with Path(path).open(encoding='utf-8') as f:
        for line in f:
            if not line.strip():continue
            event=json.loads(line)
            if set(event)!={'sequence','quote'}:raise ValueError('Unexpected replay keys')
            feed.publish(event['sequence'],parse_quote(event['quote']))
            count+=1
    return {'observed_trace_records':count,'feed_status':feed.status()}
