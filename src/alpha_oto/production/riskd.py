"""CLI for simulated independent risk service; does NOT submit real orders."""
from __future__ import annotations
import argparse
import json
from datetime import datetime,timezone
from pathlib import Path
from .market_registry import MarketRegistry
from .market_feed import DurableQuoteFeed,parse_quote,replay_jsonl
from .risk_service import IsolatedRiskAuthority, RiskClient, open_server, parse_intent


def main(argv=None):
    ap=argparse.ArgumentParser(description='Simulation-only independent risk/quote process')
    sub=ap.add_subparsers(dest='cmd',required=True)
    p=sub.add_parser('serve')
    p.add_argument('--ledger',required=True);p.add_argument('--feed',required=True)
    p.add_argument('--registry',required=True);p.add_argument('--socket',required=True)
    p.add_argument('--initial-cash',default='10000');p.add_argument('--currency',default='USD')
    p=sub.add_parser('ingest')
    p.add_argument('--feed',required=True);p.add_argument('--trace',required=True)
    p=sub.add_parser('feed-status');p.add_argument('--feed',required=True)
    for cmd in ('health','halt','authorize'):
        p=sub.add_parser(cmd);p.add_argument('--socket',required=True)
        if cmd=='authorize':p.add_argument('--intent-json',required=True)
    args=ap.parse_args(argv)
    if args.cmd=='serve':
        authority=IsolatedRiskAuthority(args.ledger,args.feed,MarketRegistry.from_file(args.registry),
                                        initial_cash=args.initial_cash,currency=args.currency)
        try:
            server=open_server(args.socket,authority)
            try:server.serve_forever(poll_interval=.15)
            finally:
                server.server_close()
                Path(args.socket).unlink(missing_ok=True)
        finally:authority.close()
        return
    if args.cmd in ('ingest','feed-status'):
        feed=DurableQuoteFeed(args.feed)
        try:result=replay_jsonl(args.trace,feed) if args.cmd=='ingest' else feed.status()
        finally:feed.close()
    else:
        client=RiskClient(args.socket)
        if args.cmd=='authorize':
            intent=parse_intent(json.loads(Path(args.intent_json).read_text()))
            result=client.authorize(intent)
        elif args.cmd=='halt':result=client.halt()
        else:result=client.health()
    print(json.dumps(result,indent=2,sort_keys=True,default=str))

if __name__=='__main__':main()
