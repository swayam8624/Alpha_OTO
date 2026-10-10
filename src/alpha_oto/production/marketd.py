"""Bounded Coinbase Advanced Trade public L2 capture or offline trace replay.

READ ONLY: public WebSocket subscription; no authentication, account, REST
orders, withdrawals or private user channel. Trust is conditional on TLS and
venue behavior. Real-world reliability and data licensing remain external.
"""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from .market_feed import DurableQuoteFeed
from .l2_gateway import L2QuoteBridge, RawL2Journal, BookUntrusted
from .advanced_l2 import ADVANCED_PUBLIC, AdvancedL2Adapter, advanced_subscription

COINBASE_PUBLIC = 'wss://ws-feed.exchange.coinbase.com'


def replay(trace,feed_file,products,*,journal_file=None,source='exchange'):
    feed=DurableQuoteFeed(feed_file)
    journal=RawL2Journal(journal_file) if journal_file else None
    if source not in ('exchange','advanced'):
        raise ValueError('Unknown market data protocol')
    bridge=L2QuoteBridge(feed,tuple(products),journal=journal if source=='exchange' else None)
    adapter=AdvancedL2Adapter(bridge,journal=journal) if source=='advanced' else bridge
    count=0; published=0
    try:
        with Path(trace).open(encoding='utf-8') as src:
            for number,line in enumerate(src,1):
                if not line.strip():continue
                record=json.loads(line)
                if not isinstance(record,dict) or set(record)!={'received_at','message'}:
                    raise ValueError(f'Invalid replay envelope at line {number}')
                received=datetime.fromisoformat(record['received_at'])
                outcome=adapter.ingest(record['message'],received_at=received)
                count+=1
                if outcome.get('status')=='QUOTE_PUBLISHED':published+=1
        bridge.disconnect('OFFLINE_REPLAY_COMPLETE')
        return {'records':count,'published_quotes':published,'mode':'OFFLINE_REPLAY_NO_ORDERS',
                'bridge':bridge.status()}
    except BaseException:
        bridge.disconnect('REPLAY_FAILED')
        raise
    finally:feed.close()


async def capture(feed_file,raw_file,products,*,seconds=60,limit=10000,source='advanced'):
    if not 1<=seconds<=24*3600 or not 1<=limit<=2_000_000:
        raise ValueError('Invalid bounded capture duration/event limit')
    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:
        raise RuntimeError('Install free capture dependency: python -m pip install "websockets>=14,<17"') from exc
    if source not in ('advanced','exchange'):
        raise ValueError('Unknown market data protocol')
    feed=DurableQuoteFeed(feed_file)
    journal=RawL2Journal(raw_file)
    bridge=L2QuoteBridge(feed,tuple(products),journal=journal if source=='exchange' else None,min_publish_ms=250)
    adapter=AdvancedL2Adapter(bridge,journal=journal) if source=='advanced' else bridge
    count=0;published=0
    loop=asyncio.get_running_loop()
    deadline=loop.time()+seconds
    try:
        # Public market data subscription only; never connects to broker URLs.
        async with connect(ADVANCED_PUBLIC if source=='advanced' else COINBASE_PUBLIC,open_timeout=10,close_timeout=5,
                           max_size=8_000_000,max_queue=8,ping_interval=20) as ws:
            if source=='advanced':
                await ws.send(json.dumps(advanced_subscription(tuple(products),'level2')))
                await ws.send(json.dumps(advanced_subscription(tuple(products),'heartbeats')))
            else:
                await ws.send(json.dumps({'type':'subscribe','product_ids':list(products),
                                         'channels':['level2','heartbeat']}))
            while count<limit and loop.time()<deadline:
                remaining=deadline-loop.time()
                if remaining<=0:break
                raw=await asyncio.wait_for(ws.recv(),timeout=min(15,remaining))
                if not isinstance(raw,str) or len(raw.encode())>8_000_000:
                    raise BookUntrusted('Invalid WebSocket market-data frame')
                receive=datetime.now(timezone.utc)
                message=json.loads(raw)
                outcome=adapter.ingest(message,received_at=receive)
                count+=1
                if outcome.get('status')=='QUOTE_PUBLISHED':published+=1
        return {'received_messages':count,'published_quotes':published,
                'mode':'PUBLIC_COINBASE_ADVANCED_L2_READ_ONLY' if source=='advanced' else 'EXCHANGE_L2_READ_ONLY','raw_path':str(raw_file),
                'feed_path':str(feed_file),'live_orders':False}
    finally:
        # A capture ending, crashing, or disconnecting must NEVER leave a fresh
        # executable L1 quote approved for the risk daemon's next order.
        try:bridge.disconnect('CAPTURE_STOPPED_OR_DISCONNECTED')
        finally:feed.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description='Read-only Coinbase L2 / local replay; NO BROKER API')
    sub=parser.add_subparsers(dest='cmd',required=True)
    p=sub.add_parser('replay');p.add_argument('--trace',required=True)
    p.add_argument('--feed',required=True);p.add_argument('--raw-journal')
    p.add_argument('--product',action='append',required=True)
    p.add_argument('--source',choices=('exchange','advanced'),default='exchange')
    p=sub.add_parser('capture');p.add_argument('--feed',required=True)
    p.add_argument('--raw-journal',required=True);p.add_argument('--product',action='append',required=True)
    p.add_argument('--seconds',type=int,default=60);p.add_argument('--limit',type=int,default=10000)
    p.add_argument('--source',choices=('advanced','exchange'),default='advanced')
    p=sub.add_parser('verify-journal');p.add_argument('--raw-journal',required=True)
    args=parser.parse_args(argv)
    if args.cmd=='replay':
        result=replay(args.trace,args.feed,args.product,journal_file=args.raw_journal,source=args.source)
    elif args.cmd=='capture':
        result=asyncio.run(capture(args.feed,args.raw_journal,args.product,
                                   seconds=args.seconds,limit=args.limit,source=args.source))
    else:
        result=RawL2Journal(args.raw_journal).verify()
    print(json.dumps(result,indent=2,sort_keys=True))


if __name__=='__main__':
    try:
        main()
    except BookUntrusted as exc:
        print(f'MARKET_FEED_QUARANTINED: {exc}',file=sys.stderr)
        raise SystemExit(2)
