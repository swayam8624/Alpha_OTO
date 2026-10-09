"""ATLAS TailHunter command-line entry point; intentionally NO order routing."""
import argparse
import csv
import json
from dataclasses import asdict
from pathlib import Path
from .core import Config, candidates, load_quotes, replay, save_quotes
from .demo import make_demo


def make_config(args):
    return Config(
        capital=args.capital,
        per_trade_risk=args.per_trade_risk,
        daily_risk=args.daily_risk,
        fixed_fee_per_order=args.fee_per_order,
        additional_cost_bps=args.other_cost_bps,
        min_votes=args.min_votes,
        max_hold_minutes=args.max_hold_minutes,
    )


def show_replay(quotes, cfg, output=None):
    trades, summary = replay(quotes, cfg)
    print(json.dumps({"summary": summary, "trades": [asdict(t) for t in trades]}, indent=2))
    if output:
        fieldnames = list(TradeFields)
        with open(output, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for trade in trades:
                record = asdict(trade)
                record["scout_votes"] = ";".join(trade.scout_votes)
                writer.writerow(record)


TradeFields = (
    "contract", "signal_time", "entry_time", "exit_time", "entry_ask",
    "exit_bid", "lots", "lot_size", "premium_at_risk", "charges", "pnl",
    "realized_multiple_before_charges", "oracle_best_bid_multiple",
    "exit_reason", "scout_votes"
)


def main():
    parser = argparse.ArgumentParser(
        prog="tailhunter",
        description="Offline option tail-event research. NEVER places live orders.",
    )
    subs = parser.add_subparsers(dest="command", required=True)
    for command in ("demo", "scan", "replay"):
        sub = subs.add_parser(command)
        if command != "demo":
            sub.add_argument("quotes_csv", help="L1 option snapshots in documented CSV schema")
        if command == "demo":
            sub.add_argument("--save-demo", help="Save explicitly synthetic quote CSV")
        sub.add_argument("--capital", type=float, default=100_000.0)
        sub.add_argument("--per-trade-risk", type=float, default=0.0025)
        sub.add_argument("--daily-risk", type=float, default=0.01)
        sub.add_argument("--fee-per-order", type=float, default=20.0)
        sub.add_argument("--other-cost-bps", type=float, default=0.0)
        sub.add_argument("--max-hold-minutes", type=int, default=90)
        sub.add_argument("--min-votes", type=int, default=3)
        if command == "replay":
            sub.add_argument("--trades-out", help="Optional report CSV")
    args = parser.parse_args()
    cfg = make_config(args)
    if not (cfg.capital > 0 and 0 < cfg.per_trade_risk <= 0.05
            and 0 < cfg.daily_risk <= 0.10 and cfg.max_hold_minutes >= 1
            and cfg.min_votes >= 1 and cfg.fixed_fee_per_order >= 0
            and cfg.additional_cost_bps >= 0):
        parser.error("Invalid capital / risk / fees / hold / vote settings")
    data = make_demo() if args.command == "demo" else load_quotes(args.quotes_csv)
    if args.command == "demo":
        print("!!! FULLY ARTIFICIAL DEMO. NOT A MARKET BACKTEST OR PREDICTION. !!!")
        if args.save_demo:
            save_quotes(args.save_demo, data)
            print(f"Saved artificial quotes to: {Path(args.save_demo).resolve()}")
    if args.command == "scan":
        signals = [asdict(sig) for sig, _entry, _future in candidates(data, cfg)]
        print(json.dumps({"research_signals": signals, "live_orders": 0}, indent=2))
    else:
        show_replay(data, cfg, getattr(args, "trades_out", None))


if __name__ == "__main__":
    main()
