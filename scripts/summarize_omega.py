"""Render summarized validated research numbers, not LLM guesses."""
import json
import sys
from pathlib import Path

root=Path(sys.argv[1] if len(sys.argv)>1 else 'artifacts/omega')
for file in (root/'quant_tournament.json',root/'cross_ml'/'cross_asset_research.json',root/'stress.json',root/'pairs_research.json'):
    if not file.exists():
        print('Not available:',file);continue
    d=json.loads(file.read_text())
    print('\n===',file,'===')
    print('Status:',d['status'])
    if 'validation' in d:
        print('Selected shadow:',d['chosen_shadow_agent'])
        for a in d['validation'][:5]:
            print(a['agent'], 'score:',round(a['average_excess_risk_score'],5),
                  'positive folds:',a['positive_excess_folds'],
                  'fills:',a['validation_fills'])
        print('Holdout:',d['holdout'] and {
              k: v for k,v in d['holdout'].items() if k in ('excess_net_return','excess_return_uncertainty')})
    if 'candidate_reports' in d:
        print('Selected shadow:',d['selected'])
        for a in sorted(d['candidate_reports'],key=lambda a:a['score'],reverse=True)[:5]:
            print(a['model'],a['horizon'],a['min_edge'],'score',round(a['score'],5),
                  'positive folds',a['positive_excess_folds'],'fills',a['validation_fills'])
        print('Holdout:', d['holdout'])
    if 'hedge' in d:
        print('Cointegration verified:',d['hedge']['cointegration_verified'])
        print('Selected segment:',d.get('selected_segment'))
        print('Holdout:',d.get('holdout'))
    if 'variants' in d:
        for name,r in d['variants'].items():
            print(name,'return',round(100*r['agent']['net_return'],3),'%',
                  'drawdown',round(100*r['agent']['max_drawdown'],3),'%')
print('\nNOTE: These are retrospective research numbers, not investment signals or guaranteed returns.')
