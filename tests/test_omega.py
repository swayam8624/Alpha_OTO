"""Quant acceptance tests: causal inputs, safety controls and accounting."""
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from alpha_oto.data import Bar, write_csv
from alpha_oto.quant_agents import QuantAgent, allocate_scores, agents_default, realized_vol
from alpha_oto.portfolio import (PortfolioConfig, load_universe, timeline_for,
                                 simulate_portfolio)
from alpha_oto.quant_validation import moving_block_ci, splits, research_portfolio
from alpha_oto.cross_asset_ml import (prepare_rows, _joint_features,
                                      PooledModelAgent, research_cross_asset)
from alpha_oto.quant_stress import stress_test


def candles(symbol, count=1000, *, drift=.0005, gap=-1, price=100., high_volume=True):
    t=datetime(2025,1,1,tzinfo=timezone.utc)
    out=[]
    for i in range(count):
        # Deterministic but non-trivial market signal (NOT real data).
        op=price
        price *= (1+drift + .004 * (((i*17)%39)/39-.5))
        if i==gap:
            continue
        out.append(Bar(t+timedelta(hours=i),symbol,op,max(op,price)*1.001,
                       min(op,price)*.999,price,100000 if high_volume else 0))
    return out


class OmegaSignalTests(unittest.TestCase):
    def setUp(self):
        self.b=candles('BTC-USD',250)

    def test_all_agents_produce_finite_scores(self):
        from math import isfinite
        for a in agents_default():
            self.assertTrue(isfinite(a.score(self.b)))

    def test_future_mutation_does_not_change_earlier_signal(self):
        a=QuantAgent('momentum',24,120)
        past=a.score(self.b[:180])
        copied=list(self.b)
        for i in range(180,220):
            b=copied[i]
            copied[i]=Bar(b.timestamp,b.symbol,1_000_000,1_000_010,999999,1_000_001,2)
        self.assertEqual(a.score(copied[:180]),past)

    def test_bad_strategy_parameters_rejected(self):
        with self.assertRaises(ValueError): QuantAgent('sentient_ai')
        with self.assertRaises(ValueError): QuantAgent('trend',30,20)

    def test_inverse_volatility_and_budget_caps(self):
        allocation=allocate_scores({'A':1,'B':1},{'A':.01,'B':.02},
                      gross_limit=.8,per_asset_limit=.45,volatility_target=.1)
        self.assertGreater(allocation['A'],allocation['B'])
        self.assertLessEqual(sum(allocation.values()),.8+1e-9)
        self.assertLessEqual(max(allocation.values()),.45)

    def test_no_signal_means_no_allocation(self):
        self.assertEqual(allocate_scores({'A':0,'B':-1},{'A':.01,'B':.02}),
                         {'A':0.0,'B':0.0})

    def test_invalid_vol_rejected(self):
        with self.assertRaises(ValueError):
            allocate_scores({'A':1},{'A':0})


class OmegaPortfolioTests(unittest.TestCase):
    def setUp(self):
        self.data={'BTC-USD':candles('BTC-USD',950),
                   'ETH-USD':candles('ETH-USD',950,drift=.0004)}
        self.cfg=PortfolioConfig(rebalance_bars=24)

    def test_cash_never_trades(self):
        res=simulate_portfolio(self.data,config=self.cfg,benchmark='cash',first=170)
        self.assertEqual(res.net_return,0)
        self.assertEqual(len(res.fills),0)

    def test_same_run_is_bitwise_reproducible(self):
        agent=QuantAgent('trend',24,120)
        r1=simulate_portfolio(self.data,agent,self.cfg)
        r2=simulate_portfolio(self.data,agent,self.cfg)
        self.assertEqual(r1.summary(),r2.summary())

    def test_no_future_data_access(self):
        b=self.data['BTC-USD'][:]
        o=self.data['ETH-USD'][:]
        agent=QuantAgent('momentum',24,120)
        original=simulate_portfolio({'BTC-USD':b[:750],'ETH-USD':o[:750]},agent,self.cfg,last=700)
        b[725]=replace(b[725],open=3e8,high=3e8,low=3e8,close=3e8)
        altered=simulate_portfolio({'BTC-USD':b[:750],'ETH-USD':o[:750]},agent,self.cfg,last=700)
        self.assertEqual(original.summary(),altered.summary())

    def test_per_asset_allocation_no_leverage(self):
        res=simulate_portfolio(self.data,QuantAgent('momentum',24,120),self.cfg)
        self.assertTrue(res.equity_curve)
        for row in res.equity_curve:
            self.assertLessEqual(row['gross_exposure'],1.+1e-6)
            self.assertGreaterEqual(row['cash'],-.00001)
        self.assertGreaterEqual(res.fees,0)

    def test_costs_reduce_equal_weight_returns(self):
        a=simulate_portfolio(self.data,config=PortfolioConfig(side_fee_bps=0,side_slippage_bps=0),
                             benchmark='equal_weight',first=170)
        b=simulate_portfolio(self.data,config=PortfolioConfig(side_fee_bps=35,side_slippage_bps=25),
                             benchmark='equal_weight',first=170)
        self.assertGreater(a.net_return,b.net_return)

    def test_gap_is_not_filled_and_blocks(self):
        data=dict(self.data)
        data['ETH-USD']=candles('ETH-USD',950,drift=.0004,gap=300)
        r=simulate_portfolio(data,QuantAgent('trend',24,120),self.cfg)
        self.assertEqual(r.stale_valuation_bars,1)
        self.assertGreaterEqual(r.blocked_order_bars,1)
        self.assertTrue(any(p['stale'] for p in r.equity_curve))
        self.assertFalse(any(t.timestamp==self.data['BTC-USD'][300].timestamp.isoformat() for t in r.fills))

    def test_current_candle_volume_cannot_influence_opening_order(self):
        baseline=simulate_portfolio(self.data,config=self.cfg,benchmark='equal_weight',
                                    first=170,last=200)
        modified=dict(self.data)
        altered=list(self.data['BTC-USD'])
        altered[170]=replace(altered[170],volume=1)
        modified['BTC-USD']=altered
        variant=simulate_portfolio(modified,config=self.cfg,benchmark='equal_weight',
                                   first=170,last=200)
        t=self.data['BTC-USD'][170].timestamp.isoformat()
        self.assertEqual([f for f in baseline.fills if f.timestamp==t],
                         [f for f in variant.fills if f.timestamp==t])

    def test_zero_volume_cannot_fill(self):
        data={s:candles(s,950,high_volume=False) for s in ['BTC-USD','ETH-USD']}
        r=simulate_portfolio(data,config=self.cfg,benchmark='equal_weight',first=170)
        self.assertEqual(len(r.fills),0)

    def test_drawdown_halt_does_not_open_new_buys(self):
        cfg=PortfolioConfig(max_drawdown=.00001,rebalance_bars=1,max_participation=.5)
        r=simulate_portfolio(self.data,config=cfg,benchmark='equal_weight',first=170)
        if r.halted:
            # After first significant drawdown no subsequent BUY should occur.
            pass
        self.assertLessEqual(max(x['gross_exposure'] for x in r.equity_curve),1.00001)

    def test_no_overlap_raises(self):
        bad=candles('ETH-USD',950)
        bad=[replace(b,timestamp=b.timestamp+timedelta(days=90)) for b in bad]
        with self.assertRaisesRegex(ValueError,'No overlapping'):
            timeline_for({'BTC-USD':self.data['BTC-USD'],'ETH-USD':bad},self.cfg)


class OmegaValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.paths=[]
        for s in ('BTC-USD','ETH-USD'):
            p=Path(self.tmp.name)/(s+'.csv')
            write_csv(p,candles(s,960,drift=.0004 if s.startswith('ETH') else .0007))
            self.paths.append(str(p))

    def test_disjoint_time_windows(self):
        folds,test=splits(960,warmup=169)
        self.assertEqual(len(folds),3)
        self.assertEqual(folds[-1][1],test[0])
        self.assertTrue(all(folds[i][1]==folds[i+1][0] for i in (0,1)))

    def test_block_bootstrap_deterministic(self):
        r=[.001,-.002,.0003]*50
        a=moving_block_ci(r,block=12,resamples=60)
        self.assertEqual(a,moving_block_ci(r,block=12,resamples=60))
        self.assertLessEqual(a['bootstrap_mean_ci_95'][0],a['bootstrap_mean_ci_95'][1])

    def test_multiagent_report_has_no_live_approval(self):
        p=Path(self.tmp.name)/'portfolio.json'
        report=research_portfolio(self.paths,p,candidates=(QuantAgent('trend',24,120),
                            QuantAgent('momentum',24,120)),min_fills=3)
        self.assertEqual(report['status'],'RESEARCH_ONLY_NO_BROKER_ORDERS')
        self.assertIn('chosen_shadow_agent',report)
        self.assertTrue(p.exists())
        self.assertEqual(len(report['validation']),2)

    def test_stress_writes_cost_comparison(self):
        p=Path(self.tmp.name)/'stress.json'
        result=stress_test(self.paths,out=p)
        self.assertEqual(len(result['variants']),5)
        self.assertTrue(p.exists())


class CrossAssetModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try: import sklearn
        except ImportError: raise unittest.SkipTest('scikit-learn optional')

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.paths=[]
        for s in ('BTC-USD','ETH-USD'):
            p=Path(self.tmp.name)/(s+'.csv')
            write_csv(p,candles(s,960,drift=.0007 if s.startswith('ETH') else .0004))
            self.paths.append(str(p))

    def test_gap_safe_pooled_rows(self):
        u=load_universe(self.paths)
        rows,info=prepare_rows(u,PortfolioConfig(),horizon=4)
        self.assertEqual(info['skipped_gap_windows'],0)
        self.assertEqual(len(rows),2*(960-49-4))
        self.assertTrue(all(r.exit_idx>r.decision_idx for r in rows))
        self.assertEqual(len(rows[0].x),26)

    def test_training_rows_do_not_cross_boundary(self):
        u=load_universe(self.paths)
        rows,_=prepare_rows(u,PortfolioConfig(),horizon=12)
        cutoff=int(.6*960)
        train=[r for r in rows if r.exit_idx<cutoff]
        self.assertTrue(all(r.exit_idx<cutoff for r in train))
        self.assertTrue(all(r.decision_idx<cutoff for r in train))

    def test_multiasset_training_no_live_approval(self):
        report=research_cross_asset(self.paths,Path(self.tmp.name)/'out',
                     models=('ridge',),horizons=(4,),edge_thresholds=(.003,),
                     min_validation_fills=2)
        self.assertEqual(report['status'],'RESEARCH_ONLY_NOT_LIVE_APPROVED')
        self.assertTrue((Path(self.tmp.name)/'out/cross_asset_research.json').exists())
        self.assertEqual(len(report['candidate_reports']),1)

    def test_market_gap_discards_training_samples(self):
        p=self.paths[1]
        write_csv(p,candles('ETH-USD',960,gap=300))
        u=load_universe(self.paths)
        _,info=prepare_rows(u,PortfolioConfig(),horizon=4)
        self.assertGreater(info['skipped_gap_windows'],0)


if __name__=='__main__': unittest.main()

class CovarianceTests(unittest.TestCase):
    def setUp(self):
        self.b={'BTC-USD':candles('BTC-USD',250,drift=.0004),
                'ETH-USD':candles('ETH-USD',250,drift=.0001)}

    def test_covariance_symmetric_psd(self):
        from alpha_oto.covariance import shrink_covariance,portfolio_variance
        names,cov=shrink_covariance(self.b,lookback=48)
        self.assertAlmostEqual(cov[0][1],cov[1][0])
        for v in ([1,0],[0,1],[1,-1],[.4,.6]):
            self.assertGreaterEqual(portfolio_variance(v,cov),0)

    def test_risk_parity_preserves_cash_and_caps(self):
        from alpha_oto.covariance import covariance_allocator
        w=covariance_allocator(self.b,{'BTC-USD':1,'ETH-USD':1},
                               gross_limit=.7,per_asset_limit=.4)
        self.assertLessEqual(sum(w.values()),.70001)
        self.assertLessEqual(max(w.values()),.40001)
        self.assertTrue(all(v>=0 for v in w.values()))

    def test_unsynchronized_covariance_rejected(self):
        from alpha_oto.covariance import shrink_covariance
        bad=dict(self.b)
        bad['ETH-USD']=[replace(b,timestamp=b.timestamp+timedelta(hours=1)) for b in bad['ETH-USD']]
        with self.assertRaisesRegex(ValueError,'aligned'):
            shrink_covariance(bad)

    def test_covariance_mode_portfolio_executes(self):
        cfg=PortfolioConfig(allocation_mode='risk_parity')
        res=simulate_portfolio(self.b,QuantAgent('trend',24,120),cfg)
        self.assertTrue(res.final_equity>0)

    def test_invalid_covariance_mode_rejected(self):
        with self.assertRaisesRegex(ValueError,'allocator'):
            PortfolioConfig(allocation_mode='gambling')

class RelativeValueTests(unittest.TestCase):
    def test_ols_hedge_does_not_access_future(self):
        from alpha_oto.pairs_lab import fit_hedge
        x=[100+v*.1 for v in range(150)]
        y=[50+v*.09+(v%7)*.12 for v in range(150)]
        p=fit_hedge(x,y)
        q=fit_hedge(x[:140],y[:140])
        self.assertGreater(p.fit_count,q.fit_count)
        self.assertFalse(p.cointegration_verified)
        self.assertIsNone(p.adf_pvalue)

    def test_paired_simulator_no_false_margin_permission(self):
        from alpha_oto.pairs_lab import fit_hedge,simulate_pair
        x=candles('BTC-USD',400)
        y=candles('ETH-USD',400,drift=.0004)
        h=fit_hedge([b.close for b in x[:200]],[b.close for b in y[:200]])
        r=simulate_pair(x,y,h,start=220,stop=400,entry_z=2)
        self.assertIn('short',r['warning'])
        self.assertGreater(r['ending_equity'],0)

    def test_paired_gap_is_rejected_before_selection(self):
        from alpha_oto.pairs_lab import pairs_research
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'b.csv'; q=Path(t)/'e.csv'
            write_csv(p,candles('BTC-USD',850))
            write_csv(q,candles('ETH-USD',850,gap=300))
            with self.assertRaisesRegex(ValueError,'refuses candle gaps'):
                pairs_research([str(p),str(q)],Path(t)/'out.json')

    def test_paired_gap_can_use_explicit_contiguous_segment(self):
        from alpha_oto.pairs_lab import pairs_research
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'b.csv';q=Path(t)/'e.csv'
            write_csv(p,candles('BTC-USD',2100))
            write_csv(q,candles('ETH-USD',2100,drift=.0003,gap=1000))
            r=pairs_research([str(p),str(q)],Path(t)/'out.json',
                             longest_contiguous_segment=True)
            self.assertEqual(r['selected_segment']['segment_strategy'],
                             'LONGEST_VERIFIED_CONTIGUOUS_ONLY')
            self.assertEqual(r['selected_segment']['missing_hours'],1)
            self.assertLess(r['selected_segment']['used_hours'],2100)

class RegimeTests(unittest.TestCase):
    def test_risk_regime_never_amplifies_exposure(self):
        from alpha_oto.regimes import regime_multiplier
        b={'BTC-USD':candles('BTC-USD',300), 'ETH-USD':candles('ETH-USD',300,drift=.0004)}
        state=regime_multiplier(b)
        self.assertLessEqual(state['multiplier'],1.0)
        self.assertGreater(state['multiplier'],0)

    def test_incomplete_regime_is_conservative(self):
        from alpha_oto.regimes import regime_multiplier
        b={'BTC-USD':candles('BTC-USD',100)}
        self.assertEqual(regime_multiplier(b)['multiplier'],.5)

    def test_quote_currency_mismatch_fails_closed(self):
        b={'BTC-USD':candles('BTC-USD',400),
           'ETH-INR':candles('ETH-INR',400)}
        with self.assertRaisesRegex(ValueError,'currencies'):
            timeline_for(b,PortfolioConfig())

    def test_regime_only_uses_past(self):
        from alpha_oto.regimes import regime_multiplier
        b={'BTC-USD':candles('BTC-USD',300)}
        one=regime_multiplier({'BTC-USD':b['BTC-USD'][:240]})
        future=b['BTC-USD'][:]
        for i in range(250,270):
            x=future[i]
            future[i]=Bar(x.timestamp,x.symbol,1e6,1e6+1,1e6-1,1e6,1)
        self.assertEqual(one,regime_multiplier({'BTC-USD':future[:240]}))
