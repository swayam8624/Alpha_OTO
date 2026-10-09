"""Zero-broker-credentials local research CLI."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from .backtest import ExecutionSettings, simulate
from .data import coinbase_candles, read_csv, write_csv
from .evolution import run_tournament
from .local_ai import summarize_locally
from .reinvestment import ReinvestmentPlan
from .strategies import Trend


def _save(path: str, payload: dict):
    dest = Path(path)
    dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(payload,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    print(f"Saved: {dest}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="alpha-oto",description="Local-first quant research. NO LIVE ORDERS.")
    sub = p.add_subparsers(dest="cmd",required=True)
    get = sub.add_parser("fetch-coinbase",help="Download read-only public crypto candles")
    get.add_argument("--product",default="BTC-USD")
    get.add_argument("--days",type=int,default=14)
    get.add_argument("--granularity",type=int,default=3600)
    get.add_argument("--out",required=True)
    sim = sub.add_parser("simulate",help="Replay trend strategy on a local CSV")
    sim.add_argument("--csv",required=True)
    sim.add_argument("--out",default="artifacts/simulation.json")
    evolve = sub.add_parser("tournament",help="Train local ML + agent validation + untouched test")
    evolve.add_argument("--csv",required=True)
    evolve.add_argument("--out",default="artifacts/tournament.json")
    local = sub.add_parser("local-report",help="Optional localhost Ollama briefing")
    local.add_argument("--json",required=True)
    local.add_argument("--model",default="qwen2.5:3b")
    reinvest = sub.add_parser("reinvest",help="Calculate HUMAN-APPROVAL-ONLY hardware budget")
    reinvest.add_argument("--realized-profit",type=float,required=True)
    reinvest.add_argument("--tax-reserve",type=float,required=True)
    reinvest.add_argument("--liquid-cash",type=float,required=True)
    reinvest.add_argument("--cash-floor",type=float,required=True)
    reinvest.add_argument("--rate",type=float,default=.20)
    args = p.parse_args(argv)
    if args.cmd == "fetch-coinbase":
        if args.days <= 0 or args.days > 90:
            p.error("--days must be from 1 to 90 (bounded public API usage)")
        end = datetime.now(timezone.utc)
        start = end-timedelta(days=args.days)
        bars = coinbase_candles(args.product,start,end,args.granularity)
        write_csv(args.out,bars)
        print(f"Downloaded {len(bars)} public candles for {args.product}; check source terms")
    elif args.cmd == "simulate":
        bars = read_csv(args.csv)
        result = simulate(bars,Trend())
        _save(args.out,result.summary())
        print(json.dumps(result.summary(),indent=2))
    elif args.cmd == "tournament":
        bars = read_csv(args.csv)
        result = run_tournament(bars)
        _save(args.out,result.to_dict())
        print(json.dumps({"selected":result.selected,"status":result.status,
                          "untouched_test":result.untouched_test},indent=2))
    elif args.cmd == "local-report":
        print(summarize_locally(json.loads(Path(args.json).read_text()),model=args.model))
    elif args.cmd == "reinvest":
        print(json.dumps(ReinvestmentPlan(args.realized_profit,args.tax_reserve,
                    args.liquid_cash,args.cash_floor,args.rate).budget(),indent=2))

if __name__ == "__main__":
    main()
