"""Execution-aware historical quote replay for long-option tail-event research.

IMPORTANT MODEL LIMITATIONS
---------------------------
1. Quotes are observations, NOT promises of fill. At the next snapshot we
   *assume* one can buy at the ask and sell at the bid if quoted size suffices.
   Queue position, latency, stale quotes and market impact remain unmodeled.
2. The current scouts are deterministic baselines, NOT calibrated AI forecasts.
   Never interpret a vote as a probability or as investment advice.
3. Backtest fees are only rough configurable placeholders, NOT the exhaustive
   Indian F&O statutory charges/taxes (which depend on contracts and dates).
4. MFE/oracle multiple is hindsight ONLY. It must never influence an entry or
   exit decision. Realized results come from a predefined trailing-exit rule.
5. This research module intentionally contains NO broker/exchange order API.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Iterable
import csv
import math


@dataclass(frozen=True)
class Quote:
    timestamp: datetime  # timezone-aware; one snapshot per minute recommended
    contract: str         # includes strike, right, expiry; exact unique ID
    right: str            # CE or PE
    expiry: str           # ISO yyyy-mm-dd
    underlying: float
    bid: float            # Rupees PER UNDERLYING UNIT, not per lot
    ask: float
    bid_lots: int         # executable quantity shown, in whole option lots
    ask_lots: int
    volume: float         # volume in this snapshot interval, not cumulative
    iv: float             # implied volatility as fraction (0.25 = 25%)
    lot_size: int         # number of underlying units in one option lot
    event_public: int = 0  # 1 ONLY if the event was public by this timestamp

    @property
    def midpoint(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_fraction(self) -> float:
        return (self.ask - self.bid) / self.ask if self.ask > 0 else math.inf

    def valid(self) -> bool:
        return (
            self.timestamp.tzinfo is not None
            and self.underlying > 0
            and self.bid > 0
            and self.ask >= self.bid
            and self.bid_lots >= 0
            and self.ask_lots >= 0
            and self.volume >= 0
            and 0 <= self.iv <= 10
            and self.lot_size >= 1
            and self.right in ("CE", "PE")
            and self.expiry >= self.timestamp.date().isoformat()
        )


@dataclass(frozen=True)
class Config:
    capital: float = 100_000.0
    per_trade_risk: float = 0.0025  # max premium + entry fee / capital
    daily_risk: float = 0.01       # maximum TOTAL premium + entry fees per day
    fixed_fee_per_order: float = 20.0
    additional_cost_bps: float = 0.0  # extra cost on buy AND sell notional
    min_ask: float = 0.20
    max_ask: float = 15.0
    max_spread_fraction: float = 0.30
    min_votes: int = 3
    lookback: int = 6
    max_hold_minutes: int = 90
    stop_fraction: float = 0.35  # after entering, exit below 35% of entry ask
    trail_activation_multiple: float = 4.0
    trailing_peak_fraction: float = 0.50


@dataclass(frozen=True)
class Signal:
    timestamp: str
    contract: str
    votes: tuple[str, ...]
    ask: float
    spread_fraction: float


@dataclass(frozen=True)
class Trade:
    contract: str
    signal_time: str
    entry_time: str
    exit_time: str
    entry_ask: float
    exit_bid: float
    lots: int
    lot_size: int
    premium_at_risk: float
    charges: float
    pnl: float
    realized_multiple_before_charges: float
    oracle_best_bid_multiple: float
    exit_reason: str
    scout_votes: tuple[str, ...]


def load_quotes(path: str) -> list[Quote]:
    """Load a broker-independent, timestamped L1 option quote CSV.

    This deliberately requires actual bid/ask and displayed sizes, rather than
    OHLC option candles. Using daily/minute OHLC highs as sell fills yields
    massively optimistic results for ultra-cheap options.
    """
    quotes = []
    with open(path, "r", newline="", encoding="utf-8") as fh:
        for line, row in enumerate(csv.DictReader(fh), start=2):
            try:
                item = Quote(
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    contract=row["contract"], right=row["right"].upper(),
                    expiry=row["expiry"], underlying=float(row["underlying"]),
                    bid=float(row["bid"]), ask=float(row["ask"]),
                    bid_lots=int(row["bid_lots"]), ask_lots=int(row["ask_lots"]),
                    volume=float(row["volume"]), iv=float(row["iv"]),
                    lot_size=int(row["lot_size"]),
                    event_public=int(row.get("event_public") or 0),
                )
                if not item.valid():
                    raise ValueError("non-tradable or inconsistent quote")
                quotes.append(item)
            except (KeyError, ValueError) as exc:
                raise ValueError(f"Invalid quote on CSV line {line}: {exc}") from exc
    if not quotes:
        raise ValueError("Input has no valid quotes")
    return sorted(quotes, key=lambda x: (x.contract, x.timestamp))


def save_quotes(path: str, quotes: Iterable[Quote]) -> None:
    fields = ["timestamp", "contract", "right", "expiry", "underlying",
              "bid", "ask", "bid_lots", "ask_lots", "volume", "iv",
              "lot_size", "event_public"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for item in quotes:
            record = asdict(item)
            record["timestamp"] = item.timestamp.isoformat()
            writer.writerow(record)


def scout_votes(history: list[Quote], cfg: Config) -> tuple[str, ...]:
    """Four transparent, independent research heuristics, not learned models.

    Every feature uses data no later than history[-1]. This function never
    looks at the next bar or the eventual payout, preventing label leakage.
    The distinct vote names support future replacement with validated ML.
    """
    if len(history) < cfg.lookback:
        return ()
    now, prev, past = history[-1], history[-2], history[-4]
    direction = 1 if now.right == "CE" else -1
    r1 = direction * (now.underlying / prev.underlying - 1.0)
    r3 = direction * (now.underlying / past.underlying - 1.0)
    prev_volumes = [h.volume for h in history[:-1] if h.volume > 0]
    volume_ratio = now.volume / max(median(prev_volumes), 1e-9) if prev_volumes else 0
    option_momentum = now.midpoint / prev.midpoint if prev.midpoint > 0 else 0
    iv_change = now.iv - past.iv

    votes = []
    if r1 > 0.001 and r3 > 0.0035:
        votes.append("directional_acceleration")
    if volume_ratio >= 3.0 and r3 > 0.002:
        votes.append("volume_shock")
    if iv_change >= 0.015 and r3 > 0.001:
        votes.append("iv_repricing")
    if option_momentum >= 1.20 and r3 > 0.002:
        votes.append("option_momentum")
    if now.event_public and r3 > 0.002:
        votes.append("public_event_context")
    return tuple(votes)


def eligible_quote(q: Quote, cfg: Config) -> bool:
    return (q.valid() and cfg.min_ask <= q.ask <= cfg.max_ask
            and q.spread_fraction <= cfg.max_spread_fraction
            and q.bid_lots >= 1 and q.ask_lots >= 1 and q.volume > 0)


def candidates(quotes: list[Quote], cfg: Config) -> list[tuple[Signal, Quote, list[Quote]]]:
    """Find potential entries using only the history preceding the next quote.

    A candidate signal at index i assumes entry no earlier than index i+1.
    One opportunity per contract per day avoids repeated same-day attempts.
    This is intentionally conservative compared to a hyperactive swarm.
    """
    groups = defaultdict(list)
    for q in quotes:
        groups[(q.contract, q.timestamp.date())].append(q)
    proposed = []
    for (_contract, _day), rows in groups.items():
        rows.sort(key=lambda q: q.timestamp)
        for i in range(cfg.lookback - 1, len(rows) - 2):
            q, entry = rows[i], rows[i + 1]
            if not eligible_quote(q, cfg) or not eligible_quote(entry, cfg):
                continue
            if (entry.timestamp - q.timestamp) > timedelta(minutes=3):
                continue  # stale-data gap or missing snapshots
            history = rows[max(0, i - cfg.lookback + 1):i + 1]
            votes = scout_votes(history, cfg)
            if len(votes) >= cfg.min_votes:
                signal = Signal(q.timestamp.isoformat(), q.contract, votes,
                                q.ask, q.spread_fraction)
                proposed.append((signal, entry, rows[i + 2:]))
                break
    proposed.sort(key=lambda item: (item[0].timestamp, item[0].contract))
    return proposed


def simulate_exit(entry: Quote, later: list[Quote], lots: int,
                  cfg: Config) -> tuple[Quote, float, str] | None:
    """Execute fixed stop/trail/time rules. Keep oracle MFE separate.

    Oracle maximum is computed afterward from observed future bid quotes, and
    can NEVER be used by the rule to decide a sale. It is a retrospective
    *upper bound* under optimistic next-snapshot liquidity assumptions.
    """
    last_allowed = entry.timestamp + timedelta(minutes=cfg.max_hold_minutes)
    valid_later = [q for q in later
                   if entry.timestamp < q.timestamp <= last_allowed
                   and q.bid > 0 and q.bid_lots >= lots]
    if not valid_later:
        return None  # DO NOT fabricate a final exit or zero loss
    peak_bid = entry.bid
    for q in valid_later:
        peak_bid = max(peak_bid, q.bid)
        if q.bid <= cfg.stop_fraction * entry.ask:
            return q, max(x.bid for x in valid_later), "premium_stop"
        if (peak_bid >= cfg.trail_activation_multiple * entry.ask
                and q.bid <= peak_bid * cfg.trailing_peak_fraction):
            return q, max(x.bid for x in valid_later), "trailing_exit"
    return valid_later[-1], max(x.bid for x in valid_later), "time_or_data_end"


def replay(quotes: list[Quote], cfg: Config) -> tuple[list[Trade], dict]:
    """Simulate purchases at NEXT quoted ask and predefined bid-based exits.

    Premium-at-risk is restricted by an account-wide daily and per-trade cap.
    Fees are not assumed to be refundable if the premium becomes worthless.
    We refuse candidates lacking a later liquid quote instead of asserting
    an attainable exit; such skipped events require separate quality audit.
    """
    trades: list[Trade] = []
    daily_spent = defaultdict(float)
    skipped = defaultdict(int)
    for signal, entry, later in candidates(quotes, cfg):
        day = entry.timestamp.date().isoformat()
        max_risk_rupees = cfg.capital * cfg.per_trade_risk
        remaining_daily = cfg.capital * cfg.daily_risk - daily_spent[day]
        max_budget = min(max_risk_rupees, remaining_daily)
        one_lot_premium = entry.ask * entry.lot_size
        max_lots = math.floor((max_budget - cfg.fixed_fee_per_order)
                              / one_lot_premium)
        lots = min(max_lots, entry.ask_lots)
        if lots < 1:
            skipped["budget_or_lot_size"] += 1
            continue
        result = simulate_exit(entry, later, lots, cfg)
        if result is None:
            skipped["no_executable_exit_quote"] += 1
            continue
        out, best_bid, reason = result
        buy_value = entry.ask * entry.lot_size * lots
        sell_value = out.bid * out.lot_size * lots
        variable_fees = (buy_value + sell_value) * cfg.additional_cost_bps / 10_000
        total_charges = 2 * cfg.fixed_fee_per_order + variable_fees
        pnl = sell_value - buy_value - total_charges
        daily_spent[day] += buy_value + cfg.fixed_fee_per_order
        trades.append(Trade(
            contract=entry.contract, signal_time=signal.timestamp,
            entry_time=entry.timestamp.isoformat(),
            exit_time=out.timestamp.isoformat(),
            entry_ask=entry.ask, exit_bid=out.bid,
            lots=lots, lot_size=entry.lot_size,
            premium_at_risk=buy_value + cfg.fixed_fee_per_order,
            charges=total_charges, pnl=pnl,
            realized_multiple_before_charges=out.bid / entry.ask,
            oracle_best_bid_multiple=best_bid / entry.ask,
            exit_reason=reason, scout_votes=signal.votes,
        ))
    stats = {
        "candidate_signals": len(candidates(quotes, cfg)),
        "executed_simulated_trades": len(trades),
        "skipped": dict(skipped),
        "gross_total_pnl_inr_after_modelled_costs": round(sum(t.pnl for t in trades), 2),
        "realized_100x_count": sum(t.realized_multiple_before_charges >= 100 for t in trades),
        "oracle_100x_count": sum(t.oracle_best_bid_multiple >= 100 for t in trades),
        "note": "Synthetic/demo data and hindsight oracle results do not prove predictive performance.",
    }
    return trades, stats
