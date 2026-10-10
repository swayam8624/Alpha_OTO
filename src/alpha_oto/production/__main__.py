"""Offline production-core CLI. All order flow is simulated, never real."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json

from . import D, Instrument, Quote, OrderIntent, SimulatedBroker, LedgerStore, SimulationGateway
from .contracts import UnknownSubmission
from .broker_durable import DurableSimulatedBroker


def _instrument():
    return Instrument('BTC-USD','SIMULATED','USD',D('.01'),D('.001'),D('2'))


def _render(data):
    print(json.dumps(data,indent=2,default=str))


def main():
    parser=argparse.ArgumentParser(description='OFFLINE production-core emulator. NO LIVE TRADES.')
    sub=parser.add_subparsers(dest='command',required=True)
    for command in ('demo','status','health','durable-start','durable-recover',
                    'durable-complete','durable-status'):
        p=sub.add_parser(command)
        p.add_argument('--db',default='artifacts/production/simulation.sqlite3',
                       help='Local simulated account SQLite path')
        if command.startswith('durable-'):
            p.add_argument('--broker-db',default='artifacts/production/remote_simulator.sqlite3',
                           help='SEPARATE durable fake broker account SQLite file')
    args=parser.parse_args()
    store=LedgerStore(args.db,instruments=(_instrument(),))
    broker=None
    try:
        if args.command=='status':
            _render(store.summary());return
        if args.command=='health':
            from .health import inspect
            _render(inspect(store));return
        if args.command.startswith('durable-'):
            broker=DurableSimulatedBroker(args.broker_db)
            gateway=SimulationGateway(store,broker)
            if args.command=='durable-status':
                from .health import inspect_durable_pair
                _render({'safety':'SIMULATOR_ONLY_LIVE_ORDERS_IMPOSSIBLE',
                         'audit':inspect_durable_pair(store,broker),
                         'ledger':store.summary(),'remote':broker.verify()})
                return
            if args.command=='durable-start':
                if store.orders() or broker.snapshot()['orders']:
                    raise SystemExit('Existing remote or local order records. Use durable-recover, not start.')
                now=datetime.now(timezone.utc)
                q=Quote('BTC-USD',D('99.99'),D('100'),D('10'),D('10'),now,now)
                intent=OrderIntent('durable-smoke-001','BTC-USD','BUY',D('.5'),D('100'),now,
                                   'durable-fault-test','frozen-v1')
                gateway.propose(intent,q,now)
                broker.accept_then_timeout=True
                try:
                    gateway.dispatch(intent)
                except UnknownSubmission:
                    pass  # Expected simulated lost acknowledgment.
                _render({'event':'BROKER_ACCEPTED_ACK_LOST','mode':'SIMULATOR_ONLY',
                         'local_state':store.order(intent.client_id)['state'],
                         'broker_calls':broker.calls,'remote_orders':broker.verify()['orders']})
                return
            gateway.reconcile()
            order=store.order('durable-smoke-001')
            if order is None:
                raise SystemExit('No saved demo order. Run durable-start first.')
            if args.command=='durable-recover':
                if D(order['filled'])==0 and order['state']=='ACKNOWLEDGED':
                    broker.fill(order['client_id'],D('.2'),D('100'),fill_id='durable-fill-1')
                gateway.reconcile()
                _render({'event':'AFTER_RESTART_RECOVERED_AND_PARTIALLY_FILLED',
                         'broker_calls':broker.calls,'order':store.order(order['client_id']),
                         'cash':store.account()['cash']})
                return
            if args.command=='durable-complete':
                remaining=D(order['quantity'])-D(order['filled'])
                if remaining>0 and order['state'] in ('ACKNOWLEDGED','PARTIAL'):
                    broker.fill(order['client_id'],remaining,D('100'),fill_id='durable-fill-final')
                result=gateway.reconcile()
                _render({'event':'AFTER_ANOTHER_RESTART_FULLY_RECONCILED',
                         'broker_calls':broker.calls,'reconciliation':result,
                         'order':store.order(order['client_id']),
                         'cash':store.account()['cash']})
                return
        now=datetime.now(timezone.utc)
        broker=SimulatedBroker()
        gateway=SimulationGateway(store,broker)
        if store.orders():
            print('Existing journal detected. Demo refuses to overwrite or synthesize broker recovery.')
            _render(store.summary());return
        q=Quote('BTC-USD',D('99.99'),D('100'),D('10'),D('10'),now,now)
        intent=OrderIntent('smoke-001','BTC-USD','BUY',D('0.5'),D('100'),now,'production-smoke','v1')
        gateway.propose(intent,q,now)
        gateway.dispatch(intent)
        broker.fill(intent.client_id,D('.2'),D('100'),fill_id='sim-1')
        gateway.reconcile(now=now)
        broker.fill(intent.client_id,D('.3'),D('100'),fill_id='sim-2')
        _render(gateway.reconcile(now=now))
        _render(store.summary())
    finally:
        if broker is not None and hasattr(broker,'close'):
            broker.close()
        store.close()


if __name__=='__main__':
    main()
