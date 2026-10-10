"""Offline public Advanced Trade L2 protocol and capture regressions."""
import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from alpha_oto.production.advanced_l2 import (
    AdvancedL2Adapter, advanced_subscription, safe_provider_error, ADVANCED_PUBLIC
)
from alpha_oto.production.l2_gateway import L2QuoteBridge, BookUntrusted, RawL2Journal
from alpha_oto.production.market_feed import DurableQuoteFeed
from alpha_oto.production.marketd import replay, capture
from alpha_oto.production.contracts import RiskRejected

T = datetime(2026, 10, 10, 5, 30, tzinfo=timezone.utc)


def stamp(sec=0):
    return (T+timedelta(seconds=sec)).isoformat().replace('+00:00','Z')


def snap(seq=0, symbol='BTC-USD'):
    return {'channel':'l2_data','sequence_num':seq,'timestamp':stamp(),
            'events':[{'type':'snapshot','product_id':symbol,'updates':[
                {'side':'bid','price_level':'99.5','new_quantity':'4','event_time':'1970-01-01T00:00:00Z'},
                {'side':'offer','price_level':'100.5','new_quantity':'3','event_time':'1970-01-01T00:00:00Z'}]}]}


def update(seq=1,clock=1,symbol='BTC-USD'):
    return {'channel':'l2_data','sequence_num':seq,'timestamp':stamp(clock),
            'events':[{'type':'update','product_id':symbol,'updates':[
                {'side':'bid','price_level':'99.8','new_quantity':'5','event_time':stamp(clock)},
                {'side':'offer','price_level':'100.2','new_quantity':'6','event_time':stamp(clock)}]}]}


class AdvancedTradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir=Path(self.tmp.name)
        self.feed=DurableQuoteFeed(self.dir/'quotes.db')
        self.addCleanup(self.feed.close)
        self.bridge=L2QuoteBridge(self.feed,('BTC-USD',))
        self.raw=RawL2Journal(self.dir/'raw.jsonl')
        self.adapter=AdvancedL2Adapter(self.bridge,journal=self.raw)

    def test_single_channel_subscriptions_no_secrets(self):
        self.assertEqual(advanced_subscription(('BTC-USD',),'level2'),
            {'type':'subscribe','product_ids':['BTC-USD'],'channel':'level2'})
        self.assertEqual(advanced_subscription(('BTC-USD',),'heartbeats')['channel'],'heartbeats')
        with self.assertRaises(ValueError):advanced_subscription(('BTC-USD',),'user')

    def test_snapshot_requires_timestamped_update(self):
        self.assertEqual(self.adapter.ingest(snap(),received_at=T)['status'],'BOOK_SNAPSHOT')
        with self.assertRaises(RiskRejected):self.feed.latest('BTC-USD')
        self.assertEqual(self.adapter.ingest(update(),received_at=T+timedelta(seconds=1))['status'],'QUOTE_PUBLISHED')
        self.assertEqual(str(self.feed.latest('BTC-USD').ask),'100.2')
        self.assertEqual(self.raw.verify()['entries'],2)
        self.assertEqual(next(self.raw.entries())['message']['channel'],'l2_data')

    def test_heartbeat_counts_in_connection_sequence(self):
        self.adapter.ingest(snap(),received_at=T)
        self.adapter.ingest({'channel':'heartbeats','sequence_num':1,'events':[]},received_at=T+timedelta(seconds=1))
        self.adapter.ingest(update(2,2),received_at=T+timedelta(seconds=2))
        self.assertEqual(self.feed.sequence_for('BTC-USD'),1)

    def test_gap_quarantines_existing_quote(self):
        self.adapter.ingest(snap(),received_at=T)
        self.adapter.ingest(update(),received_at=T+timedelta(seconds=1))
        with self.assertRaisesRegex(BookUntrusted,'sequence discontinuity'):
            self.adapter.ingest(update(3,2),received_at=T+timedelta(seconds=2))
        with self.assertRaises(RiskRejected):self.feed.latest('BTC-USD')

    def test_duplicate_sequence_is_failure(self):
        self.adapter.ingest(snap(),received_at=T)
        with self.assertRaises(BookUntrusted):self.adapter.ingest(snap(),received_at=T)
        self.assertTrue(self.bridge.suspended)

    def test_invalid_sequence_types_refused(self):
        for seq in (None,True,'1',-1):
            with self.subTest(seq=seq):
                bridge=L2QuoteBridge(self.feed,('BTC-USD',))
                adapter=AdvancedL2Adapter(bridge)
                with self.assertRaises(BookUntrusted):
                    adapter.ingest(snap(seq),received_at=T)

    def test_exchange_error_displays_reason(self):
        with self.assertRaisesRegex(BookUntrusted,'requires authentication'):
            self.adapter.ingest({'type':'error','message':'Failed to subscribe',
              'reason':'level2 requires authentication'},received_at=T)
        self.assertTrue(self.bridge.suspended)
        self.assertEqual(self.raw.verify()['entries'],1)

    def test_unknown_product_refused(self):
        with self.assertRaisesRegex(BookUntrusted,'Unapproved product'):
            self.adapter.ingest(snap(symbol='X-USD'),received_at=T)

    def test_old_event_time_refused(self):
        self.adapter.ingest(snap(),received_at=T)
        m=update()
        m['events'][0]['updates'][0]['event_time']=stamp(-120)
        with self.assertRaises(BookUntrusted):
            self.adapter.ingest(m,received_at=T+timedelta(seconds=1))

    def test_crossed_order_book_refused(self):
        self.adapter.ingest(snap(),received_at=T)
        m=update();m['events'][0]['updates'][0]['price_level']='101'
        with self.assertRaises(BookUntrusted):
            self.adapter.ingest(m,received_at=T+timedelta(seconds=1))

    def test_unknown_channel_refused(self):
        with self.assertRaises(BookUntrusted):
            self.adapter.ingest({'channel':'user','sequence_num':0},received_at=T)

    def test_bad_level_side_refused(self):
        self.adapter.ingest(snap(),received_at=T)
        m=update();m['events'][0]['updates'][0]['side']='buy'
        with self.assertRaises(BookUntrusted):
            self.adapter.ingest(m,received_at=T+timedelta(seconds=1))

    def test_journal_failure_quarantines_previous_quote(self):
        self.adapter.ingest(snap(),received_at=T)
        self.adapter.ingest(update(),received_at=T+timedelta(seconds=1))
        with patch.object(self.raw,'append',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.adapter.ingest(update(2,2),received_at=T+timedelta(seconds=2))
        with self.assertRaises(RiskRejected):self.feed.latest('BTC-USD')

    def test_advanced_offline_replay(self):
        trace=self.dir/'advanced.jsonl'
        rows=[{'received_at':stamp(),'message':snap()},
              {'received_at':stamp(1),'message':update()}]
        trace.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        out=self.dir/'replay.db'
        result=replay(trace,out,['BTC-USD'],source='advanced')
        self.assertEqual(result['published_quotes'],1)
        f=DurableQuoteFeed(out)
        try:
            with self.assertRaises(RiskRejected):f.latest('BTC-USD')
        finally:f.close()

    def test_existing_exchange_replay_unchanged(self):
        trace=Path(__file__).parent/'fixtures/coinbase_l2_trace.jsonl'
        self.assertEqual(replay(trace,self.dir/'old.db',['BTC-USD'],source='exchange')['published_quotes'],2)

    def test_live_capture_default_uses_public_advanced(self):
        incoming=[snap(),update()]
        now=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        for row in incoming[1]['events'][0]['updates']:
            row['event_time']=now
        urls=[];sent=[]
        class WS:
            async def __aenter__(self):return self
            async def __aexit__(self,*a):return False
            async def send(self,data):sent.append(json.loads(data))
            async def recv(self):return json.dumps(incoming.pop(0))
        def connect(url,**kwargs):urls.append(url);return WS()
        with patch('websockets.asyncio.client.connect',connect):
            result=asyncio.run(capture(self.dir/'capture.db',self.dir/'capture.jsonl',
                ['BTC-USD'],seconds=4,limit=2))
        self.assertEqual(urls,[ADVANCED_PUBLIC])
        self.assertEqual([x['channel'] for x in sent],['level2','heartbeats'])
        self.assertEqual(result['published_quotes'],1)
        self.assertTrue(all('jwt' not in x for x in sent))

    def test_live_capture_refusal_journaled_and_blocked(self):
        class WS:
            async def __aenter__(self):return self
            async def __aexit__(self,*a):return False
            async def send(self,data):pass
            async def recv(self):return json.dumps({'type':'error',
                'message':'Failed to subscribe','reason':'access denied'})
        target=self.dir/'failed.db'
        with patch('websockets.asyncio.client.connect',return_value=WS()):
            with self.assertRaisesRegex(BookUntrusted,'access denied'):
                asyncio.run(capture(target,self.dir/'failed.jsonl',['BTC-USD'],seconds=4,limit=2))
        f=DurableQuoteFeed(target)
        try:
            self.assertEqual(f.db.execute('SELECT COUNT(*) FROM quote_incidents').fetchone()[0],1)
            with self.assertRaises(RiskRejected):f.latest('BTC-USD')
        finally:f.close()

    def test_error_details_bounded(self):
        self.assertLess(len(safe_provider_error({'message':'Failed','reason':'Z'*900})),350)


if __name__=='__main__':unittest.main()
