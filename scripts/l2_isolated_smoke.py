#!/usr/bin/env python3
"""Synthetic Coinbase-L2 FORMAT -> separate risk process -> durable FAKE broker.

This is a no-money operational integration test. The prices and deposits are
fabricated, the market session is SIMULATED, and no broker API is contacted.
"""
from __future__ import annotations
import argparse
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from alpha_oto.production import D, OrderIntent, LedgerStore, SimulationGateway
from alpha_oto.production.contracts import RiskRejected, UnknownSubmission
from alpha_oto.production.broker_durable import DurableSimulatedBroker
from alpha_oto.production.health import inspect_durable_pair
from alpha_oto.production.market_feed import DurableQuoteFeed
from alpha_oto.production.market_registry import MarketRegistry
from alpha_oto.production.l2_gateway import L2QuoteBridge,RawL2Journal
from alpha_oto.production.market_watch import inspect_authority,append_watch_audit
from alpha_oto.production.risk_service import RiskClient, RiskUnavailable, SeparatedSimulationGateway


def run(outdir,registry_file):
    folder=Path(outdir)
    folder.mkdir(parents=True,exist_ok=False)
    registry=MarketRegistry.from_file(registry_file)
    if list(registry.markets)!=['BTC-USD'] or registry.markets['BTC-USD'].venue!='SIMULATED':
        raise ValueError('Only SIMULATED BTC-USD supported in the offline safety demo')
    feed_file=folder/'quotes.sqlite3'
    raw=RawL2Journal(folder/'capture.jsonl')
    feed=DurableQuoteFeed(feed_file)
    bridge=L2QuoteBridge(feed,('BTC-USD',),journal=raw)
    t=datetime.now(timezone.utc)
    snap={'type':'snapshot','product_id':'BTC-USD',
          'bids':[['99.99','5'],['99.95','10']],
          'asks':[['100.00','4'],['100.05','10']]}
    update={'type':'l2update','product_id':'BTC-USD',
            'time':t.isoformat(),'changes':[['buy','99.99','5'],['sell','100.00','4']]}
    bridge.ingest(snap,received_at=t-timedelta(milliseconds=30))
    bridge.ingest(update,received_at=t)
    local=folder/'local.sqlite3';remote=folder/'remote.sqlite3'
    home=tempfile.TemporaryDirectory(prefix='alphaoto-l2risk-')
    sock=Path(home.name)/'s'
    with (folder/'riskd.log').open('w') as logfile:
        daemon=subprocess.Popen([sys.executable,'-m','alpha_oto.production.riskd','serve',
             '--ledger',str(local),'--feed',str(feed_file),'--registry',str(registry_file),
             '--socket',str(sock)],stdout=logfile,stderr=subprocess.STDOUT,env=os.environ.copy())
        client=RiskClient(sock)
        try:
            for _ in range(180):
                if daemon.poll() is not None:
                    raise RuntimeError('Risk daemon terminated prematurely')
                if sock.exists():
                    try:
                        client.health()
                        break
                    except RiskUnavailable:pass
                time.sleep(.025)
            else:raise RuntimeError('Risk daemon did not become healthy')
            store=LedgerStore(local,instruments=(registry.markets['BTC-USD'].instrument(),))
            broker=DurableSimulatedBroker(remote)
            try:
                gw=SeparatedSimulationGateway(client,SimulationGateway(store,broker))
                intent=OrderIntent('l2-paper-test-001','BTC-USD','BUY',D('.5'),D('100'),t,
                                   'l2-format-smoke','immutable-test-1')
                approved=gw.propose(intent)
                broker.accept_then_timeout=True
                try:gw.dispatch(intent)
                except UnknownSubmission:pass
                gw.gateway.reconcile()
                broker.fill(intent.client_id,D('.2'),D('100'),fill_id='l2smoke-a')
                gw.gateway.reconcile()
                broker.fill(intent.client_id,D('.3'),D('100'),fill_id='l2smoke-b')
                reconciled=gw.gateway.reconcile()
                healthy_before=inspect_authority(client,min_free_bytes=0)
                if healthy_before['status']!='GREEN':
                    raise RuntimeError('Expected simulated risk service GREEN before disconnect')
                bridge.disconnect('TEST_CONNECTION_TERMINATED')
                unhealthy_after=inspect_authority(client,min_free_bytes=0)
                if unhealthy_after['status']!='CRITICAL':
                    raise RuntimeError('Disconnected market data did not trip external monitor')
                try:
                    new_order=OrderIntent('l2-paper-test-002','BTC-USD','BUY',D('.5'),D('100'),
                       datetime.now(timezone.utc),'l2-format-smoke','immutable-test-1')
                    client.authorize(new_order)
                except RiskRejected:pass
                else:raise RuntimeError('Post-disconnect risk wrongly authorized another simulated buy')
                report={'mode':'SIMULATION_ONLY_NO_LIVE_ORDERS',
                        'authorized_before_disconnect':approved,'submitted_orders':broker.calls,
                        'remote_fills':len(broker.snapshot()['fills']),
                        'reconciliation':reconciled,'account_audit':inspect_durable_pair(store,broker),
                        'market_source':'FABRICATED_COINBASE_L2_FORMAT_FOR_INTEGRATION_TEST',
                        'raw_journal':raw.verify(),
                        'watch_before':healthy_before,'watch_after':unhealthy_after}
                append_watch_audit(folder/'watch_history.jsonl',healthy_before)
                append_watch_audit(folder/'watch_history.jsonl',unhealthy_after)
                (folder/'run_report.json').write_text(json.dumps(report,indent=2,default=str)+'\n')
                if broker.calls!=1 or report['account_audit']['status']!='GREEN':
                    raise RuntimeError('Fake broker and ledger did not reconcile')
                return report
            finally:
                broker.close();store.close()
        finally:
            bridge.disconnect('INTEGRATION_PROCESS_EXIT')
            feed.close()
            daemon.terminate()
            try:daemon.wait(timeout=3)
            except subprocess.TimeoutExpired:daemon.kill();daemon.wait()
            sock.unlink(missing_ok=True)
            home.cleanup()


def main():
    p=argparse.ArgumentParser(description='Synthetic L2 -> isolated risk -> simulated durable broker')
    p.add_argument('--outdir',required=True)
    p.add_argument('--registry',default='config/simulator_market.json')
    args=p.parse_args()
    print(json.dumps(run(args.outdir,Path(args.registry).resolve()),indent=2,default=str))

if __name__=='__main__':main()
