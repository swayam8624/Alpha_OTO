"""Local smoke-test and inspection CLI; NEVER places any real order."""
import argparse
from datetime import datetime,timezone
from decimal import Decimal
import json
from pathlib import Path
from . import D,Instrument,Quote,OrderIntent,RiskLimits,SimulatedBroker,LedgerStore,SimulationGateway


def main():
    p=argparse.ArgumentParser(description='Offline production-core reliability simulator. NO LIVE TRADES.')
    sub=p.add_subparsers(dest='command',required=True)
    demo=sub.add_parser('demo');demo.add_argument('--db',default='artifacts/production/simulation.sqlite3')
    status=sub.add_parser('status');status.add_argument('--db',default='artifacts/production/simulation.sqlite3')
    health=sub.add_parser('health');health.add_argument('--db',default='artifacts/production/simulation.sqlite3')
    a=p.parse_args()
    inst=Instrument('BTC-USD','SIMULATED','USD',Decimal('.01'),Decimal('.001'),Decimal('2'))
    store=LedgerStore(a.db,instruments=(inst,))
    try:
        if a.command=='status':
            print(json.dumps(store.summary(),indent=2,default=str));return
        if a.command=='health':
            from .health import inspect
            print(json.dumps(inspect(store),indent=2));return
        now=datetime.now(timezone.utc)
        broker=SimulatedBroker()
        gateway=SimulationGateway(store,broker)
        if store.orders():
            print('Existing journal detected. Demo refuses to overwrite or synthesize broker recovery.')
            print(json.dumps(store.summary(),indent=2));return
        q=Quote('BTC-USD',D('99.99'),D('100'),D('10'),D('10'),now,now)
        intent=OrderIntent('smoke-001','BTC-USD','BUY',D('0.5'),D('100'),now,'production-smoke','v1')
        gateway.propose(intent,q,now)
        gateway.dispatch(intent)
        broker.fill(intent.client_id,D('.2'),D('100'),fill_id='sim-1')
        gateway.reconcile(now=now)
        broker.fill(intent.client_id,D('.3'),D('100'),fill_id='sim-2')
        print(json.dumps(gateway.reconcile(now=now),indent=2))
        print(json.dumps(store.summary(),indent=2))
    finally:store.close()

if __name__=='__main__':main()
