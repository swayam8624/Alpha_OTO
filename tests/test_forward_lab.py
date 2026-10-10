"""Zero-network real-time evidence protocol acceptance and regression checks."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import unittest

from alpha_oto.data import Bar, read_csv, write_csv
from alpha_oto.forward_lab import freeze_forward, advance_forward, status_forward, _paper_fill
from alpha_oto.incremental import update_coinbase
from alpha_oto.demo import synthetic_bars


class ForwardLabTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.p=Path(self.tmp.name)
        raw=synthetic_bars(240)
        self.symbols=['BTC-USD','ETH-USD']
        self.files=[];self.history=[]
        for s,ratio in [('BTC-USD',1),('ETH-USD',1.2)]:
            candles=[Bar(b.timestamp,s,b.open*ratio,b.high*ratio,b.low*ratio,b.close*ratio,b.volume)
                     for b in raw]
            self.history.append(candles)
            path=self.p/(s+'.csv')
            write_csv(path,candles);self.files.append(str(path))
        self.step=timedelta(hours=1)
        self.freeze_time=raw[-1].timestamp+self.step+timedelta(minutes=20)
        self.state=str(self.p/'frozen.json')

    def freeze(self):
        return freeze_forward(self.files,self.state,now=self.freeze_time)

    def extend(self,n=3):
        for s,path in zip(self.symbols,self.files):
            bars=read_csv(path)
            for i in range(n):
                b=bars[-1]; next_=b.timestamp+self.step
                bars.append(Bar(next_,s,b.close*1.01,b.close*1.021,b.close*.995,
                                b.close*1.012, max(b.volume,100)))
            write_csv(path,bars)

    def test_freeze_does_not_score_existing_history(self):
        x=self.freeze()
        self.assertEqual(x['status'],'RESEARCH_ONLY_NO_BROKER_ORDERS')
        state=json.loads(Path(self.state).read_text())
        self.assertIsNone(state['pending_intent'])
        self.assertTrue(all(b['fills']==0 for b in state['books'].values()))
        self.assertEqual(len(state['events']),1)

    def test_no_backfilled_paper_profit_from_old_history(self):
        self.freeze()
        report=advance_forward(self.files,self.state,now=self.freeze_time)
        self.assertTrue(report['new_future_intent'])
        self.assertEqual(report['new_completed_market_times'],0)
        self.assertTrue(all(x['paper_fills']==0 for x in report['strategies'].values()))
        pending=report['pending_future_intent']
        self.assertGreater(datetime.fromisoformat(pending),self.freeze_time)

    def test_precommit_then_observe_exactly_once(self):
        self.freeze()
        first=advance_forward(self.files,self.state,now=self.freeze_time)
        target=datetime.fromisoformat(first['pending_future_intent'])
        self.extend(2)
        second=advance_forward(self.files,self.state,now=target+self.step+timedelta(minutes=5))
        self.assertTrue(second['paper_fill_observed'])
        bench=second['strategies']['equal_weight_benchmark']
        self.assertGreater(bench['paper_fills'],0)
        third=advance_forward(self.files,self.state,now=target+self.step+timedelta(minutes=6))
        self.assertFalse(third['paper_fill_observed'])
        self.assertEqual(bench['paper_fills'],third['strategies']['equal_weight_benchmark']['paper_fills'])
        self.assertTrue(any(e['kind']=='PAPER_FILL' for e in json.loads(Path(self.state).read_text())['events']))

    def test_never_trade_if_target_quote_missing(self):
        self.freeze()
        x=advance_forward(self.files,self.state,now=self.freeze_time)
        target=datetime.fromisoformat(x['pending_future_intent'])
        self.extend(2)
        eth=read_csv(self.files[1]);eth=[b for b in eth if b.timestamp!=target]
        write_csv(self.files[1],eth)
        after=advance_forward(self.files,self.state,now=target+self.step+timedelta(minutes=5))
        self.assertFalse(after['paper_fill_observed'])
        self.assertEqual(after['strategies']['equal_weight_benchmark']['paper_fills'],0)
        self.assertTrue(any(e['kind']=='PAPER_FILL_SKIPPED' for e in json.loads(Path(self.state).read_text())['events']))

    def test_freeze_does_not_overwrite(self):
        self.freeze()
        with self.assertRaises(FileExistsError):self.freeze()

    def test_initial_history_edit_is_detected(self):
        self.freeze()
        bars=read_csv(self.files[0]);b=bars[30]
        bars[30]=Bar(b.timestamp,b.symbol,b.open,b.high*1.2,b.low,b.close,b.volume)
        write_csv(self.files[0],bars)
        with self.assertRaisesRegex(ValueError,'Frozen history'):
            advance_forward(self.files,self.state,now=self.freeze_time)

    def test_journal_edit_is_detected(self):
        self.freeze()
        state=json.loads(Path(self.state).read_text());state['events'][0]['payload']['research_only']=False
        Path(self.state).write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError,'journal broken'):
            status_forward(self.state)

    def test_change_to_already_observed_future_bar_is_detected(self):
        self.freeze()
        advance_forward(self.files,self.state,now=self.freeze_time)
        self.extend(3)
        when=self.freeze_time+self.step*3
        advance_forward(self.files,self.state,now=when)
        bars=read_csv(self.files[0]);b=bars[-2]
        bars[-2]=Bar(b.timestamp,b.symbol,b.open,b.high*1.5,b.low,b.close,b.volume)
        write_csv(self.files[0],bars)
        with self.assertRaisesRegex(ValueError,'Previously observed forward candle'):
            advance_forward(self.files,self.state,now=when+timedelta(minutes=1))

    def test_stale_feed_wont_propose(self):
        self.freeze()
        x=advance_forward(self.files,self.state,now=self.freeze_time+timedelta(days=4))
        self.assertFalse(x['new_future_intent'])
        self.assertEqual(x['pending_future_intent'],None)

    def test_no_future_data_at_freeze(self):
        with self.assertRaisesRegex(ValueError,'unfinished bar'):
            freeze_forward(self.files,self.state,now=self.freeze_time-timedelta(hours=2))

    def test_late_drawdown_halt_blocks_precommitted_buy(self):
        self.freeze()
        report=advance_forward(self.files,self.state,now=self.freeze_time)
        state=json.loads(Path(self.state).read_text())
        target=datetime.fromisoformat(report['pending_future_intent'])
        bench=state['books']['equal_weight_benchmark']
        bench['halted']=True
        rows={s:Bar(target,s,100,103,99,101,100) for s in self.symbols}
        _paper_fill(state,state['pending_intent'],rows)
        self.assertTrue(all(units==0 for units in bench['positions'].values()))
        self.assertEqual(bench['cash'],state['starting_cash'])

    def test_no_live_permissions_through_schema(self):
        self.freeze()
        state=json.loads(Path(self.state).read_text())
        self.assertNotIn('broker',state)
        self.assertNotIn('api_key',state)
        self.assertEqual(state['safety'],'RESEARCH_ONLY_NO_BROKER_ORDERS')

    def test_bad_cash_or_universe(self):
        with self.assertRaises(ValueError):freeze_forward(self.files,self.state,now=self.freeze_time,starting_cash=-1)
        with self.assertRaises(ValueError):freeze_forward(self.files[:1],self.state,now=self.freeze_time)


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'BTC-USD.csv'
        src=synthetic_bars(200)
        self.old=[Bar(b.timestamp,'BTC-USD',b.open,b.high,b.low,b.close,b.volume) for b in src]
        write_csv(self.path,self.old)
        self.now=src[-1].timestamp+timedelta(hours=4)

    def test_append_only_and_idempotent(self):
        last=self.old[-1]
        future=[Bar(last.timestamp+timedelta(hours=i),'BTC-USD',last.open,last.high,last.low,last.close,last.volume) for i in (1,2,3)]
        original=self.path.read_bytes()
        with patch('alpha_oto.incremental.coinbase_candles',return_value=future):
            result=update_coinbase(str(self.path),now=self.now)
        self.assertEqual(result['new_bars'],3)
        self.assertEqual(len(read_csv(self.path)),len(self.old)+3)
        with patch('alpha_oto.incremental.coinbase_candles',return_value=[]):
            again=update_coinbase(str(self.path),now=self.now)
        self.assertEqual(again['new_bars'],0)

    def test_conflicting_source_is_rejected(self):
        last=self.old[-1]
        edited=Bar(last.timestamp,last.symbol,last.open,last.high*1.5,last.low,last.close,last.volume)
        before=self.path.read_bytes()
        with patch('alpha_oto.incremental.coinbase_candles',return_value=[edited]):
            with self.assertRaisesRegex(ValueError,'revised'):
                update_coinbase(str(self.path),now=self.now)
        self.assertEqual(before,self.path.read_bytes())

    def test_long_offline_gap_rejected(self):
        with self.assertRaisesRegex(ValueError,'299 bars'):
            update_coinbase(str(self.path),now=self.now+timedelta(days=30))


if __name__=='__main__':unittest.main()
