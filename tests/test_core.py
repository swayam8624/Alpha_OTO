"""All tests are offline and require no broker, cloud, API key or payment."""
import json
import math
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from alpha_oto.backtest import ExecutionSettings, simulate
from alpha_oto.data import Bar, read_csv, write_csv, validate_series
from alpha_oto.evolution import run_tournament
from alpha_oto.ml import LogisticModel, MLAgent, train_logistic, training_samples
from alpha_oto.reinvestment import ReinvestmentPlan
from alpha_oto.strategies import Trend, feature_vector
from alpha_oto.local_ai import summarize_locally
from alpha_oto.demo import synthetic_bars
from alpha_oto.watch import inspect_file, watch
from alpha_oto.model_store import build_artifact, save_artifact, load_artifact, score_unseen
from alpha_oto.registry import AgentRegistry


def synthetic(n=240):
    """Nonfinancial synthetic series; intentionally has regime changes."""
    rng = random.Random(12042)
    t = datetime(2025,1,1,tzinfo=timezone.utc)
    data=[]
    p=100.
    for i in range(n):
        o=p
        drift = .001 if i % 80 < 35 else (-.001 if i % 80 < 60 else 0)
        p = max(.1, p*(1+drift+rng.uniform(-.018,.018)))
        data.append(Bar(t+timedelta(hours=i),"SYNTHETIC-USD",o,max(o,p)*1.001,
                        min(o,p)*.999,p,1000+rng.random()*200))
    return data


class DataTests(unittest.TestCase):
    def test_roundtrip(self):
        bars=synthetic(45)
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"ohlcv.csv"
            write_csv(path,bars)
            self.assertEqual(read_csv(path),bars)

    def test_reject_duplicate_or_unordered(self):
        bars=synthetic(3)
        with self.assertRaises(ValueError):
            validate_series([bars[1],bars[0]])
        with self.assertRaises(ValueError):
            validate_series([bars[0],bars[0]])

    def test_reject_naive_timestamp(self):
        b=synthetic(1)[0]
        with self.assertRaises(ValueError):
            Bar(datetime(2025,1,1),b.symbol,b.open,b.high,b.low,b.close,b.volume)

    def test_reject_invalid_ohlc(self):
        with self.assertRaises(ValueError):
            Bar(datetime.now(timezone.utc),"X",20,19,10,15,10)


class TimeSafetyTests(unittest.TestCase):
    def test_strategy_does_not_see_current_or_future_bar(self):
        bars=synthetic(75)
        seen=[]
        class Recording:
            name="record"
            def decide(self,history):
                seen.append(history[-1].timestamp)
                return False
        simulate(bars,Recording())
        self.assertEqual(seen[0],bars[30].timestamp)
        self.assertEqual(seen[-1],bars[-2].timestamp)

    def test_target_embargo_is_strict(self):
        bars=synthetic(100)
        cutoff=75
        samples=list(training_samples(bars,begin=20,end=cutoff))
        self.assertEqual(len(samples),cutoff-22)
        edited=bars[:]
        for i in range(cutoff,len(bars)):
            b=edited[i]
            edited[i]=Bar(b.timestamp,b.symbol,b.open*5,b.high*5,b.low*5,b.close*5,b.volume)
        self.assertEqual(samples,list(training_samples(edited,begin=20,end=cutoff)))

    def test_features_use_latest_complete_bar(self):
        b=synthetic(50)
        self.assertEqual(feature_vector(b[:21]),feature_vector(b[:21]))
        self.assertNotEqual(feature_vector(b[:21]),feature_vector(b[:22]))


class BacktestTests(unittest.TestCase):
    def test_holding_has_costs(self):
        bars=synthetic(80)
        class Always:
            name="always"
            def decide(self,history): return True
        frictionless=simulate(bars,Always(),ExecutionSettings(side_fee_bps=0,side_slippage_bps=0))
        realistic=simulate(bars,Always(),ExecutionSettings(side_fee_bps=15,side_slippage_bps=10))
        self.assertLess(realistic.final_equity,frictionless.final_equity)
        self.assertEqual(realistic.round_trips,1)
        self.assertGreater(realistic.total_fees,0)

    def test_never_trade(self):
        class Never:
            name="never"
            def decide(self,history):return False
        r=simulate(synthetic(60),Never())
        self.assertEqual(r.net_return,0)
        self.assertEqual(r.round_trips,0)

    def test_first_fill_at_next_open(self):
        bars=synthetic(80)
        class Always:
            name="always"
            def decide(self,history):return True
        r=simulate(bars,Always())
        self.assertEqual(r.fills[0].timestamp,bars[31].timestamp.isoformat())
        self.assertAlmostEqual(r.fills[0].price,bars[31].open*1.001)

    def test_invalid_position_fraction_rejected(self):
        with self.assertRaises(ValueError):
            ExecutionSettings(position_fraction=1.5)


class AgentTests(unittest.TestCase):
    def test_local_model_training_and_serialization(self):
        bars=synthetic(220)
        samples=training_samples(bars,begin=20,end=130,cost_bps=20)
        model=train_logistic(samples)
        model2=LogisticModel.from_dict(json.loads(json.dumps(model.to_dict())))
        pred=model.predict(feature_vector(bars[:155]))
        self.assertAlmostEqual(pred,model2.predict(feature_vector(bars[:155])))
        self.assertTrue(0 < pred < 1)
        self.assertIsInstance(MLAgent(model).decide(bars[:155]),bool)

    def test_empty_training_rejected(self):
        with self.assertRaises(ValueError):
            train_logistic([])

    def test_tournament_never_labels_live_approved(self):
        tournament=run_tournament(synthetic(250))
        self.assertIn("RESEARCH_ONLY",tournament.status)
        self.assertEqual(len(tournament.validation),5)
        self.assertIsInstance(tournament.benchmark_test,dict)
        self.assertEqual(tournament.validation_end,200)
        self.assertEqual(tournament.train_end,150)

    def test_reinvestment_profit_only(self):
        negative=ReinvestmentPlan(-1000,500,20000,10000).budget()
        positive=ReinvestmentPlan(10000,2000,20000,10000,.25).budget()
        no_liquidity=ReinvestmentPlan(10000,0,10000,10000,.5).budget()
        self.assertEqual(negative["available_for_reinvestment"],0)
        self.assertEqual(positive["available_for_reinvestment"],2000)
        self.assertEqual(no_liquidity["available_for_reinvestment"],0)

    def test_read_only_watcher_rejects_stale(self):
        bars=synthetic_bars(200)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"SYNTHETIC.csv"
            write_csv(p,bars)
            report=inspect_file(p,now=bars[-1].timestamp+timedelta(hours=49))
            self.assertFalse(report["data_current"])
            self.assertFalse(report["trade_authorized"])
            self.assertEqual(report["signal_count"],0)
            journal=Path(d)/"watch.jsonl"
            watch([str(p)],out=str(journal),once=True)
            self.assertFalse(json.loads(journal.read_text().splitlines()[0])["trade_authorized"])

    def test_synthetic_example_is_reproducible(self):
        self.assertEqual(synthetic_bars(165),synthetic_bars(165))
        self.assertEqual(synthetic_bars(165)[0].symbol,"SYNTHETIC-USD")

    def test_model_save_load_and_unseen_gate(self):
        bars=synthetic_bars(240)
        artifact=build_artifact(bars)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"model.json"
            save_artifact(p,artifact)
            with self.assertRaises(FileExistsError):
                save_artifact(p,artifact)
            restored=load_artifact(p)
            self.assertFalse(score_unseen(bars,restored)["trade_authorized"])
            with self.assertRaises(ValueError):
                score_unseen(bars[:restored["trained_candles"]],restored)

    def test_local_agent_registry_never_promotes_to_live(self):
        with tempfile.TemporaryDirectory() as d:
            registry=AgentRegistry(Path(d)/"agents.sqlite")
            try:
                for k in range(3):
                    registry.evaluate("momo",f"bt-{k}","BACKTEST",.04,.03,11)
                self.assertFalse(registry.promote_shadow("momo"))
                self.assertTrue(registry.promote_shadow("momo",human_review=True))
                self.assertEqual(registry.stage("momo"),"SHADOW")
                registry.evaluate("momo","forward-1","FORWARD",-.15,.16,20)
                self.assertEqual(registry.stage("momo"),"QUARANTINED")
            finally:
                registry.close()

    def test_cloud_llm_forbidden(self):
        with self.assertRaises(ValueError):
            summarize_locally({"some":"report"},endpoint="https://api.somecloud.com")
        with self.assertRaises(ValueError):
            summarize_locally({},endpoint="http://192.168.1.10:11434")

if __name__ == "__main__": unittest.main()
