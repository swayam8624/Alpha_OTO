#!/usr/bin/env python3
"""Two real processes + durable fake brokerage; *SIMULATION ONLY*.

Creates a new local directory; cannot access a real broker or spend money.
"""
import argparse
from datetime import datetime,timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import tempfile
import sys
import time

from alpha_oto.production import D, OrderIntent, LedgerStore, SimulationGateway
from alpha_oto.production.broker_durable import DurableSimulatedBroker
from alpha_oto.production.contracts import Quote, UnknownSubmission
from alpha_oto.production.market_feed import DurableQuoteFeed
from alpha_oto.production.market_registry import MarketRegistry
from alpha_oto.production.risk_service import RiskClient,RiskUnavailable,SeparatedSimulationGateway


def run(folder, registry_file):
    folder=Path(folder)
    folder.mkdir(parents=True,exist_ok=False)
    market=MarketRegistry.from_file(registry_file)
    if len(market.markets)!=1:raise ValueError('Smoke demo expects exactly one simulated asset')
    spec=next(iter(market.markets.values()))
    if spec.venue != 'SIMULATED' or spec.currency!='USD' or not spec.continuous:
        raise ValueError('Smoke demo only supports SIMULATED 24/7 USD market')
    ledger=folder/'local_ledger.sqlite3'
    broker_file=folder/'fake_broker.sqlite3'
    feed_file=folder/'quote_feed.sqlite3'
    # macOS Unix sockets often permit only ~104 path bytes: avoid the long
    # checkout path by allocating a private, temporary short socket path.
    socket_home=tempfile.TemporaryDirectory(prefix='alphaoto-risk-')
    sock=Path(socket_home.name)/'s'
    now=datetime.now(timezone.utc)
    bid=D('99.99');ask=D('100')
    feed=DurableQuoteFeed(feed_file)
    try:feed.publish(1,Quote(spec.symbol,bid,ask,D('10'),D('10'),now,now))
    finally:feed.close()
    risk_env=dict(os.environ)
    command=[sys.executable,'-m','alpha_oto.production.riskd','serve',
             '--ledger',str(ledger),'--feed',str(feed_file),
             '--registry',str(registry_file),'--socket',str(sock)]
    logfile=(folder/'risk_service.log').open('w')
    daemon=subprocess.Popen(command,stdout=logfile,stderr=subprocess.STDOUT,env=risk_env)
    client=RiskClient(sock)
    try:
        for _ in range(150):
            if daemon.poll() is not None:raise RuntimeError('Risk service failed: '+(folder/'risk_service.log').read_text()[-1000:])
            if sock.exists():
                try:client.health();break
                except RiskUnavailable:pass
            time.sleep(.03)
        else:raise RuntimeError('Risk authority startup timeout')
        store=LedgerStore(ledger,instruments=(spec.instrument(),))
        fake=DurableSimulatedBroker(broker_file)
        try:
            gateway=SeparatedSimulationGateway(client,SimulationGateway(store,fake))
            intent=OrderIntent('isolated-smoke-001',spec.symbol,'BUY',D('.5'),D('100'),now,'smoke-not-alpha','frozen-demo')
            permitted=gateway.propose(intent)
            fake.accept_then_timeout=True
            try:gateway.dispatch(intent)
            except UnknownSubmission:pass
            remote=fake.snapshot()
            if len(remote['orders'])!=1:raise RuntimeError('Fake broker submit failed')
            fake.accept_then_timeout=False
            reconciled=gateway.gateway.reconcile()
            fake.fill(intent.client_id,D('.2'),D('100'),fill_id='iso-partial-1')
            gateway.gateway.reconcile()
            fake.fill(intent.client_id,D('.3'),D('100'),fill_id='iso-partial-2')
            completed=gateway.gateway.reconcile()
            from alpha_oto.production.health import inspect_durable_pair
            audit=inspect_durable_pair(store,fake)
            if audit['status']!='GREEN' or fake.calls!=1:
                raise RuntimeError('Isolated simulation audit did not pass')
            result={'mode':'SIMULATOR_ONLY_NO_LIVE_ORDERS',
                    'authority':permitted,'risk_process_pid':daemon.pid,
                    'broker_calls':fake.calls,'fills':2,'cash':store.account()['cash'],
                    'account_audit':audit,'reconciliation':completed,'risk_health':client.health(),
                    'warning':'Fabricated quotes and fake USD cash; no market edge or live permissions.'}
            (folder/'run_report.json').write_text(json.dumps(result,indent=2,default=str)+'\n')
            return result
        finally:
            fake.close();store.close()
    finally:
        daemon.terminate()
        try:daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:daemon.kill();daemon.wait()
        logfile.close()
        sock.unlink(missing_ok=True)
        socket_home.cleanup()


def main():
    p=argparse.ArgumentParser(description='Separate-process offline risk + broker fault simulation')
    p.add_argument('--outdir',required=True)
    p.add_argument('--registry',default='config/simulator_market.json')
    args=p.parse_args()
    print(json.dumps(run(args.outdir,Path(args.registry).resolve()),indent=2,default=str))

if __name__=='__main__':main()
