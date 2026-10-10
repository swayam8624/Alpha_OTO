"""Offline-only tests of Coinbase-style public L2 aggregation and quarantine."""
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alpha_oto.production.contracts import D, IntegrityFailure, RiskRejected
from alpha_oto.production.market_feed import DurableQuoteFeed, SequenceGap
from alpha_oto.production.l2_gateway import L2Book, BookUntrusted, L2QuoteBridge, RawL2Journal
from alpha_oto.production.marketd import replay
from alpha_oto.production.market_watch import inspect_authority,append_watch_audit

FIXTURE=Path(__file__).parent/'fixtures'/'coinbase_l2_trace.jsonl'
RECORDS=[json.loads(v) for v in FIXTURE.read_text().splitlines()]


def received(i):return datetime.fromisoformat(RECORDS[i]['received_at'])
def message(i):return copy.deepcopy(RECORDS[i]['message'])


class L2BookTests(unittest.TestCase):
    def test_snapshot_is_not_falsely_source_timestamped(self):
        b=L2Book('BTC-USD')
        self.assertIsNone(b.apply(message(0),received(0)))
        self.assertTrue(b.synchronized)
        self.assertIsNone(b.last_exchange_time)
        self.assertEqual(max(b.bids),D('99.90'))

    def test_absolute_price_update_and_removal(self):
        b=L2Book('BTC-USD');b.apply(message(0),received(0))
        q=b.apply(message(1),received(1))
        self.assertEqual((q.bid,q.ask,q.bid_size,q.ask_size),(D('99.95'),D('100.05'),D('2.5'),D('1.25')))
        q=b.apply(message(2),received(2))
        self.assertEqual((q.bid,q.ask,q.bid_size,q.ask_size),(D('99.90'),D('100.05'),D('5'),D('2.5')))

    def test_update_requires_snapshot(self):
        with self.assertRaises(BookUntrusted):L2Book('BTC-USD').apply(message(1),received(1))

    def test_stale_exchange_time_fails_closed(self):
        b=L2Book('BTC-USD');b.apply(message(0),received(0))
        with self.assertRaises(BookUntrusted):b.apply(message(1),received(1)+timedelta(minutes=2))
        self.assertTrue(b.halted)

    def test_clock_reverse_detected(self):
        b=L2Book('BTC-USD');b.apply(message(0),received(0));b.apply(message(1),received(1))
        with self.assertRaises(BookUntrusted):b.apply(message(2),received(0))

    def test_invalid_crossed_book_cannot_publish(self):
        b=L2Book('BTC-USD');b.apply(message(0),received(0))
        invalid=message(1);invalid['changes']=[['buy','100.50','4']]
        with self.assertRaises(BookUntrusted):b.apply(invalid,received(1))
        self.assertTrue(b.halted)
        self.assertEqual(max(b.bids),D('99.90'))

    def test_price_and_size_input_limits(self):
        for value in ['NaN','Infinity','-2','0']:
            with self.subTest(value=value):
                b=L2Book('BTC-USD');m=message(0);m['asks'][0][0]=value
                with self.assertRaises(BookUntrusted):b.apply(m,received(0))
        b=L2Book('BTC-USD');m=message(0);m['bids'][0][1]='-1'
        with self.assertRaises(BookUntrusted):b.apply(m,received(0))

    def test_unexpected_extra_snapshot_rejected(self):
        b=L2Book('BTC-USD');b.apply(message(0),received(0))
        with self.assertRaises(BookUntrusted):b.apply(message(0),received(1))

    def test_malformed_change_rollback(self):
        b=L2Book('BTC-USD');b.apply(message(0),received(0))
        old=b.bids.copy();bad=message(1);bad['changes'].append(['fake','20','3'])
        with self.assertRaises(BookUntrusted):b.apply(bad,received(1))
        self.assertEqual(b.bids,old)

    def test_exchange_clock_reverse_detected(self):
        b=L2Book('BTC-USD');b.apply(message(0),received(0));b.apply(message(1),received(1))
        bad=message(2);bad['time']='2026-10-10T02:59:59Z'
        with self.assertRaises(BookUntrusted):b.apply(bad,received(2))


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.addCleanup(self.dir.cleanup)
        self.file=Path(self.dir.name)/'raw.jsonl'

    def test_append_reopen_validates(self):
        j=RawL2Journal(self.file)
        self.assertEqual(j.append(received(0),message(0)),1)
        self.assertEqual(j.append(received(1),message(1)),2)
        self.assertEqual(RawL2Journal(self.file).verify()['entries'],2)
        self.assertEqual([r['index'] for r in j.entries()],[1,2])

    def test_tampered_payload_refused(self):
        j=RawL2Journal(self.file);j.append(received(0),message(0))
        self.file.write_text(self.file.read_text().replace('99.90','777.7'))
        with self.assertRaises(IntegrityFailure):RawL2Journal(self.file)

    def test_torn_write_refused(self):
        j=RawL2Journal(self.file);j.append(received(0),message(0))
        with self.file.open('ab') as f:f.write(b'{"partial":')
        with self.assertRaises(IntegrityFailure):RawL2Journal(self.file)

    def test_deleted_open_raw_journal_refused(self):
        j=RawL2Journal(self.file);j.append(received(0),message(0))
        self.file.unlink()
        with self.assertRaises(IntegrityFailure):j.verify()

    def test_truncated_open_raw_journal_refused(self):
        j=RawL2Journal(self.file);j.append(received(0),message(0))
        self.file.write_text('')
        with self.assertRaises(IntegrityFailure):j.verify()

    def test_unsigned_message_refused(self):
        j=RawL2Journal(self.file)
        with self.assertRaises(ValueError):j.append(received(0),['wrong'])


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.addCleanup(self.dir.cleanup)
        self.feed=DurableQuoteFeed(Path(self.dir.name)/'feed.sqlite')
        self.addCleanup(self.feed.close)
        self.bridge=L2QuoteBridge(self.feed,('BTC-USD',))

    def test_first_publish_requires_timestamped_update(self):
        self.assertEqual(self.bridge.ingest(message(0),received_at=received(0))['status'],'BOOK_SNAPSHOT')
        with self.assertRaises(RiskRejected):self.feed.latest('BTC-USD')
        out=self.bridge.ingest(message(1),received_at=received(1))
        self.assertEqual(out['status'],'QUOTE_PUBLISHED')
        self.assertEqual(self.feed.latest('BTC-USD').ask,D('100.05'))
        self.assertEqual(self.feed.sequence_for('BTC-USD'),1)

    def test_reopen_bridge_does_not_use_old_snapshot(self):
        self.bridge.ingest(message(0),received_at=received(0));self.bridge.ingest(message(1),received_at=received(1))
        again=L2QuoteBridge(self.feed,('BTC-USD',))
        with self.assertRaises(BookUntrusted):again.ingest(message(2),received_at=received(2))
        with self.assertRaises(RiskRejected):self.feed.latest('BTC-USD')

    def test_unknown_product_quarantines(self):
        self.bridge.ingest(message(0),received_at=received(0));self.bridge.ingest(message(1),received_at=received(1))
        bad=message(2);bad['product_id']='ETH-USD'
        with self.assertRaises(BookUntrusted):self.bridge.ingest(bad,received_at=received(2))
        with self.assertRaises(RiskRejected):self.feed.latest('BTC-USD')

    def test_disconnect_blocks_still_fresh_quote(self):
        self.bridge.ingest(message(0),received_at=received(0));self.bridge.ingest(message(1),received_at=received(1))
        self.bridge.disconnect('TRANSPORT_LOST')
        with self.assertRaises(RiskRejected):self.feed.latest('BTC-USD')
        self.assertTrue(self.feed.status()['quotes'][0]['quarantined'])

    def test_double_disconnect_idempotent(self):
        self.bridge.disconnect('TRANSPORT_LOST')
        self.bridge.disconnect('TRANSPORT_LOST')
        with self.assertRaises(SequenceGap):self.feed.publish(1,self._fake_quote())

    def _fake_quote(self):
        from alpha_oto.production.contracts import Quote
        return Quote('BTC-USD',D('1'),D('2'),D('1'),D('1'),received(1),received(1))

    def test_control_channel_does_not_produce_price(self):
        self.assertEqual(self.bridge.ingest({'type':'heartbeat','product_id':'BTC-USD'},received_at=received(0))['status'],'CONTROL_ONLY')
        with self.assertRaises(RiskRejected):self.feed.latest('BTC-USD')

    def test_publish_throttle_preserves_integrity(self):
        self.bridge=L2QuoteBridge(self.feed,('BTC-USD',),min_publish_ms=2000)
        self.bridge.ingest(message(0),received_at=received(0))
        self.bridge.ingest(message(1),received_at=received(1))
        self.assertEqual(self.bridge.ingest(message(2),received_at=received(2))['status'],'BOOK_UPDATED_THROTTLED')
        self.assertEqual(self.feed.sequence_for('BTC-USD'),1)

    def test_raw_journal_failure_quarantines_earlier_good_price(self):
        class BrokenJournal:
            def append(self,*args,**kwargs):
                raise IntegrityFailure('disk-full')
        self.bridge.ingest(message(0),received_at=received(0))
        self.bridge.ingest(message(1),received_at=received(1))
        self.bridge.journal=BrokenJournal()
        with self.assertRaises(IntegrityFailure):
            self.bridge.ingest(message(2),received_at=received(2))
        with self.assertRaises(RiskRejected):
            self.feed.latest('BTC-USD')

    def test_replay_fixtures_to_new_feed(self):
        for i in range(3):self.bridge.ingest(message(i),received_at=received(i))
        self.assertEqual(self.feed.sequence_for('BTC-USD'),2)
        self.assertEqual(self.feed.latest('BTC-USD').bid,D('99.90'))

    def test_cli_replay_untrusted_unfinished_source_sealed(self):
        dest=Path(self.dir.name)/'cli_feed.sqlite'
        result=replay(FIXTURE,dest,['BTC-USD'])
        self.assertEqual(result['published_quotes'],2)
        feed=DurableQuoteFeed(dest)
        try:
            self.assertTrue(feed.status()['quotes'][0]['quarantined'])
            with self.assertRaises(RiskRejected):feed.latest('BTC-USD')
        finally:feed.close()



class WatchdogTests(unittest.TestCase):
    def test_green_never_implies_live_trading(self):
        class Client:
            def health(self):
                return {'status':'SIMULATION_RISK_SERVICE_OK',
                    'safe_for_new_simulated_intents':True,'live_approved':False,
                    'feed':{'verified':True,'quotes':[{'symbol':'BTC-USD','quarantined':False}]}}
        out=inspect_authority(Client(),min_free_bytes=0)
        self.assertEqual(out['status'],'GREEN')
        self.assertFalse(out['live_trading_enabled'])

    def test_socket_failure_is_critical(self):
        class Client:
            def health(self):raise ConnectionError('network down')
        out=inspect_authority(Client(),min_free_bytes=0)
        self.assertEqual(out['status'],'CRITICAL')
        self.assertTrue(any('RISK_UNREACHABLE' in x for x in out['issues']))

    def test_quarantined_feed_is_critical(self):
        class Client:
            def health(self):
                return {'status':'QUARANTINED_FEED',
                    'safe_for_new_simulated_intents':False,'live_approved':False,
                    'feed':{'verified':True,'quotes':[{'symbol':'BTC-USD','quarantined':True}]}}
        out=inspect_authority(Client(),min_free_bytes=0)
        self.assertEqual(out['status'],'CRITICAL')

    def test_disk_shortage_critical(self):
        class Client:
            def health(self):
                return {'status':'SIMULATION_RISK_SERVICE_OK',
                    'safe_for_new_simulated_intents':True,'live_approved':False,
                    'feed':{'verified':True,'quotes':[{'symbol':'BTC-USD','quarantined':False}]}}
        out=inspect_authority(Client(),min_free_bytes=10**30)
        self.assertIn('DISK_BELOW_RESERVED_FREE_SPACE',out['issues'])

    def test_audit_file_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'alerts.jsonl'
            append_watch_audit(path,{'status':'CRITICAL'})
            append_watch_audit(path,{'status':'GREEN'})
            self.assertEqual(len(path.read_text().splitlines()),2)
            self.assertEqual(json.loads(path.read_text().splitlines()[1])['status'],'GREEN')

    def test_bad_threshold_rejected(self):
        class Client:
            def health(self):return {}
        with self.assertRaises(ValueError):inspect_authority(Client(),min_free_bytes=-1)

    def test_unexpected_live_true_is_critical(self):
        class Client:
            def health(self):
                return {'status':'SIMULATION_RISK_SERVICE_OK',
                    'safe_for_new_simulated_intents':True,'live_approved':True,
                    'feed':{'verified':True,'quotes':[{'quarantined':False}]}}
        self.assertEqual(inspect_authority(Client(),min_free_bytes=0)['status'],'CRITICAL')


class SystemIntegrationTests(unittest.TestCase):
    def test_l2_to_isolated_risk_to_durable_broker_across_processes(self):
        import subprocess, sys, os
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as t:
            result=subprocess.run([sys.executable,str(root/'scripts/l2_isolated_smoke.py'),
                '--outdir',str(Path(t)/'new-run'), '--registry',
                str(root/'config/simulator_market.json')],cwd=root,
                env={**os.environ,'PYTHONPATH':str(root/'src')},
                capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr[-2000:])
            payload=json.loads(result.stdout)
            self.assertEqual(payload['submitted_orders'],1)
            self.assertEqual(payload['remote_fills'],2)
            self.assertEqual(payload['account_audit']['status'],'GREEN')
            self.assertEqual(payload['watch_before']['status'],'GREEN')
            self.assertEqual(payload['watch_after']['status'],'CRITICAL')
            self.assertFalse(payload['authorized_before_disconnect']['live_approved'])

    def test_cli_offline_replay_in_own_process(self):
        import subprocess, sys, os
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as t:
            trace=Path(t)/'feed.sqlite'
            result=subprocess.run([sys.executable,'-m','alpha_oto.production.marketd','replay',
                '--trace',str(FIXTURE),'--feed',str(trace),'--product','BTC-USD'],
                cwd=root,env={**os.environ,'PYTHONPATH':str(root/'src')},
                capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr[-2000:])
            report=json.loads(result.stdout)
            self.assertEqual(report['published_quotes'],2)
            self.assertTrue(report['bridge']['suspended'])
            feed=DurableQuoteFeed(trace)
            try:
                with self.assertRaises(RiskRejected):feed.latest('BTC-USD')
            finally:feed.close()

    def test_risk_source_l2_json_cannot_override_live_status(self):
        b=L2Book('BTC-USD')
        b.apply(message(0),received(0))
        bad=message(1);bad['time']='not-a-date'
        with self.assertRaises(BookUntrusted):b.apply(bad,received(1))
        self.assertTrue(b.halted)

    def test_feed_quarantine_survives_database_reopen(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'quote.db'
            f=DurableQuoteFeed(path)
            f.quarantine('BTC-USD','DEAD_WEBSOCKET')
            f.close()
            f=DurableQuoteFeed(path)
            try:
                with self.assertRaises(SequenceGap):f.publish(1,BridgeTests._fake_quote(None))
            finally:f.close()

    def test_exchange_error_message_quarantines(self):
        with tempfile.TemporaryDirectory() as t:
            feed=DurableQuoteFeed(Path(t)/'f.db')
            bridge=L2QuoteBridge(feed,('BTC-USD',))
            try:
                with self.assertRaises(BookUntrusted):
                    bridge.ingest({'type':'error','message':'subscription refused'},
                                  received_at=received(0))
                self.assertTrue(bridge.suspended)
            finally:feed.close()

if __name__=='__main__':unittest.main()
