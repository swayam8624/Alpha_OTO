"""Tests demonstrate absence of obvious look-ahead, gap handling, and safety gates."""
import json
import tempfile
import unittest
from pathlib import Path
from datetime import timedelta

from alpha_oto.data import Bar, write_csv
from alpha_oto.demo import synthetic_bars
from alpha_oto.quant_ml import (
    FEATURE_NAMES, feature_vector_at, make_observations,
    _extract, _folds, evaluate_signals, score_saved_model,
)


class QuantMLFeatureTests(unittest.TestCase):
    def test_future_values_cannot_change_features(self):
        bars = synthetic_bars(200)
        features = feature_vector_at(bars,60)
        last=bars[100]
        modified = bars[:]
        modified[100] = Bar(last.timestamp,last.symbol,last.open*1000,
                            last.high*1000,last.low*1000,last.close*1000,last.volume)
        self.assertEqual(features,feature_vector_at(modified,60))
        self.assertEqual(len(features),len(FEATURE_NAMES))

    def test_missing_bar_discards_windows_near_gap(self):
        bars=synthetic_bars(350)
        original, info1=make_observations(bars,horizon=4,interval_seconds=3600,side_cost_bps=25)
        without=bars[:150]+bars[151:]
        missing, info2=make_observations(without,horizon=4,interval_seconds=3600,side_cost_bps=25)
        self.assertGreater(info2['excluded_for_missing_intervals'],0)
        self.assertLess(len(missing),len(original))
        self.assertTrue(all(not (r.decision_idx<=150<=r.exit_idx and r.decision_idx>=100)
                            for r in missing))

    def test_purged_labels_do_not_cross_split(self):
        bars=synthetic_bars(1000)
        rows,_=make_observations(bars,horizon=12,interval_seconds=3600,side_cost_bps=25)
        for train_end, val_end in _folds(len(bars)):
            train = _extract(rows,0,train_end,training=True)
            val = _extract(rows,train_end,val_end,training=False)
            self.assertLess(max(r.exit_idx for r in train),train_end)
            self.assertGreaterEqual(min(r.decision_idx for r in val),train_end)
            self.assertLess(max(r.exit_idx for r in val),val_end)

    def test_no_position_without_edge(self):
        bars=synthetic_bars(200)
        rows,_=make_observations(bars,horizon=1,interval_seconds=3600,side_cost_bps=25)
        result=evaluate_signals(bars,rows,[.10]*len(rows),threshold=.60)
        self.assertEqual(result['round_trips'],0)
        self.assertEqual(result['net_return'],0.)


class QuantMLTrainingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import sklearn, joblib
        except ImportError:
            raise unittest.SkipTest('Optional research dependencies not installed')

    def test_synthetic_train_report_and_reject_reuse(self):
        from alpha_oto.quant_ml import research
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'data.csv'
            out=Path(tmp)/'model'
            write_csv(path,synthetic_bars(620))
            result=research(path,out,horizons=(1,),models=('logistic',),thresholds=(.55,))
            self.assertEqual(result['status'],'RESEARCH_ONLY_NOT_LIVE_APPROVED')
            self.assertEqual(result['data_quality_by_horizon']['1']['excluded_for_missing_intervals'],0)
            self.assertFalse(result['selected_research_configuration']['eligible_for_further_shadow_research'])
            self.assertTrue((out/'research_model.joblib').exists())
            self.assertTrue((out/'research_report.json').exists())
            manifest=json.loads((out/'model_manifest.json').read_text())
            self.assertTrue(manifest['train_label_ends_before_test'])
            with self.assertRaisesRegex(ValueError,'already included'):
                score_saved_model(path,out)


if __name__=='__main__':
    unittest.main()
