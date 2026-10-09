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
from .demo import synthetic_bars
from .watch import watch
from .audit import audit_bars
from .model_store import build_artifact, save_artifact, load_artifact, score_unseen
from .quant_ml import research as run_ml_research, score_saved_model
from .repair import repair_coinbase


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
    audit = sub.add_parser("audit-data",help="Audit local candles for gaps and incomplete periods (read-only)")
    audit.add_argument("--csv",required=True)
    audit.add_argument("--interval-seconds",type=int,default=3600)
    audit.add_argument("--session-market",action="store_true",help="Do not count closed sessions as missing 24/7 candles")
    audit.add_argument("--out",default="artifacts/data_audit.json")
    train = sub.add_parser("train-local",help="Train and save provenance-checked local logistic ML")
    train.add_argument("--csv",required=True)
    train.add_argument("--out",default="artifacts/local_model.json")
    score = sub.add_parser("score-local",help="Score an unseen completed candle, no trade")
    score.add_argument("--csv",required=True)
    score.add_argument("--model",required=True)
    sim = sub.add_parser("simulate",help="Replay trend strategy on a local CSV")
    sim.add_argument("--csv",required=True)
    sim.add_argument("--out",default="artifacts/simulation.json")
    evolve = sub.add_parser("tournament",help="Train local ML + agent validation + untouched test")
    evolve.add_argument("--csv",required=True)
    evolve.add_argument("--out",default="artifacts/tournament.json")
    local = sub.add_parser("local-report",help="Optional localhost Ollama briefing")
    local.add_argument("--json",required=True)
    local.add_argument("--model",default="qwen2.5:3b")
    demo = sub.add_parser("demo",help="Create deterministic, EXPLICITLY SYNTHETIC research candles")
    demo.add_argument("--out",default="artifacts/SYNTHETIC_ohlcv.csv")
    demo.add_argument("--bars",type=int,default=480)
    watcher = sub.add_parser("watch",help="Monitor local data without a broker or live trading")
    watcher.add_argument("--csv",action="append",required=True,help="Repeat for each market file")
    watcher.add_argument("--out",default="artifacts/watch.jsonl")
    watcher.add_argument("--interval-seconds",type=float,default=60)
    watcher.add_argument("--max-age-hours",type=float,default=48)
    watcher.add_argument("--once",action="store_true")
    ml = sub.add_parser("ml-research",help="Gap-safe chronological train/validation/holdout ML (RESEARCH ONLY)")
    ml.add_argument("--csv",required=True)
    ml.add_argument("--out",required=True,help="Local folder for model and JSON evidence")
    ml.add_argument("--models",default="logistic,histgb,forest",help="Comma separated; optional lightgbm,xgboost")
    ml.add_argument("--horizons",default="1,4,12",help="Comma-separated candle horizons")
    ml.add_argument("--interval-seconds",type=int,default=3600)
    ml.add_argument("--side-cost-bps",type=float,default=25.,help="Fees + modeled slippage per SIDE")
    ml.add_argument("--position-fraction",type=float,default=.25)
    predict = sub.add_parser("ml-predict",help="No-order prediction using locally trained model")
    predict.add_argument("--csv",required=True)
    predict.add_argument("--model-directory",required=True)
    repair = sub.add_parser("repair-coinbase",help="Re-download missing Coinbase candle intervals, never interpolate")
    repair.add_argument("--csv",required=True)
    repair.add_argument("--interval-seconds",type=int,default=3600)
    repair.add_argument("--max-missing",type=int,default=250)
    repair.add_argument("--overwrite",action="store_true",help="Replace original after fetching and validating")
    reinvest = sub.add_parser("reinvest",help="Calculate HUMAN-APPROVAL-ONLY hardware budget")
    reinvest.add_argument("--realized-profit",type=float,required=True)
    reinvest.add_argument("--tax-reserve",type=float,required=True)
    reinvest.add_argument("--liquid-cash",type=float,required=True)
    reinvest.add_argument("--cash-floor",type=float,required=True)
    reinvest.add_argument("--rate",type=float,default=.20)
    args = p.parse_args(argv)
    if args.cmd == "demo":
        if args.bars < 160 or args.bars > 100000:
            p.error("--bars must be between 160 and 100000")
        write_csv(args.out,synthetic_bars(args.bars))
        print(f"Saved deterministic SYNTHETIC candles: {args.out}; NOT market evidence")
    elif args.cmd == "audit-data":
        result = audit_bars(read_csv(args.csv), interval_seconds=args.interval_seconds,
                            continuous=not args.session_market, file_path=args.csv)
        _save(args.out,result)
        print(json.dumps(result,indent=2))
    elif args.cmd == "watch":
        watch(args.csv,interval_seconds=args.interval_seconds,out=args.out,
              once=args.once,max_age_hours=args.max_age_hours)
    elif args.cmd == "fetch-coinbase":
        # Permit meaningful long-horizon research while limiting request volume.
        limits = {60: 2, 300: 7, 900: 30, 3600: 365, 21600: 1095, 86400: 3650}
        if args.granularity not in limits or not 1 <= args.days <= limits[args.granularity]:
            p.error(f"Unsupported time range/granularity; maximum days by seconds: {limits}")
        # Exclude the current unfinished candle. Buckets are stamped by their
        # OPEN time; including the current bucket would contaminate analysis.
        seconds = int(datetime.now(timezone.utc).timestamp())
        end = datetime.fromtimestamp(seconds - seconds % args.granularity,timezone.utc)
        start = end-timedelta(days=args.days)
        bars = coinbase_candles(args.product,start,end,args.granularity)
        write_csv(args.out,bars)
        manifest = audit_bars(bars,interval_seconds=args.granularity,continuous=True,file_path=args.out)
        manifest["requested_start"] = start.isoformat()
        manifest["requested_end_exclusive"] = end.isoformat()
        # Explicitly check that the provider returned the requested window
        # endpoints; the between-first-last audit cannot detect edge gaps.
        expected_count = int((end-start).total_seconds() // args.granularity)
        manifest["requested_bars"] = expected_count
        manifest["coverage_of_requested_window"] = round(len(bars)/expected_count,8)
        if bars[0].timestamp > start or bars[-1].timestamp < end-timedelta(seconds=args.granularity):
            manifest["issues"].append("REQUEST_WINDOW_EDGE_GAP")
            manifest["quality_pass"] = False
        manifest["source"] = "Coinbase Exchange public historical candles, research use only"
        manifest["source_api"] = "https://api.exchange.coinbase.com/products/{product}/candles"
        manifest["licence_note"] = "Review data retention/training/redistribution rights before use."
        _save(args.out+".manifest.json",manifest)
        print(f"Downloaded {len(bars)} completed public candles for {args.product}; check source terms")
        print(f"Data audit: {'PASS' if manifest['quality_pass'] else 'CHECK ISSUES'}: {manifest['issues']}")
    elif args.cmd == "ml-research":
        result = run_ml_research(args.csv,args.out,
            horizons=tuple(int(x) for x in args.horizons.split(",")),
            models=tuple(x.strip() for x in args.models.split(",")),
            interval_seconds=args.interval_seconds,side_cost_bps=args.side_cost_bps,
            position_fraction=args.position_fraction)
        print(json.dumps({"selected":result["selected_research_configuration"],
             "holdout":result["holdout"],"shadow_candidate_only":result["shadow_candidate_only"],
             "status":result["status"],"report":str(Path(args.out)/"research_report.json")},indent=2))
    elif args.cmd == "ml-predict":
        print(json.dumps(score_saved_model(args.csv,args.model_directory),indent=2))
    elif args.cmd == "repair-coinbase":
        print(json.dumps(repair_coinbase(args.csv,interval_seconds=args.interval_seconds,
            max_missing=args.max_missing,overwrite=args.overwrite),indent=2))
    elif args.cmd == "train-local":
        artifact=build_artifact(read_csv(args.csv))
        save_artifact(args.out,artifact)
        print(f"Saved locally trained, uncalibrated model: {args.out}")
        print(json.dumps({k:v for k,v in artifact.items() if k!="model"},indent=2))
    elif args.cmd == "score-local":
        print(json.dumps(score_unseen(read_csv(args.csv),load_artifact(args.model)),indent=2))
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
