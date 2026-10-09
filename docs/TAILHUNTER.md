# ATLAS TailHunter — Local-First Tail-Event Research Prototype

**Status: prototype v0.1 — NO live trading, NO demonstrated edge, NO trained AI.**

This Python package is an auditable research starting point for investigating a
very specific question: *Can a signal generated from available information
identify options that may later exhibit enormous one-day price changes, and can
those changes actually be monetized after crossing spreads, lot constraints,
liquidity requirements and fees?*

The prototype intentionally contrasts two results: the hindsight maximum
observed bid multiple (oracle maximum favorable excursion, MFE) and the actual
result of a predefined trailing-stop/holding-time exit rule. The first is not
an achievable trading strategy. The second is still only a simplistic replay.

## Quick start — runs on Mac, no cloud or broker account

```bash
# From repository root (no other packages needed):
PYTHONPATH=src python3 -m tailhunter demo --save-demo artifacts/SYNTHETIC_quotes.csv
PYTHONPATH=src python3 -m tailhunter scan artifacts/SYNTHETIC_quotes.csv
PYTHONPATH=src python3 -m tailhunter replay artifacts/SYNTHETIC_quotes.csv \
  --trades-out artifacts/demo_trades.csv
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The integrated module is runnable from the Alpha_OTO root without a pip
installation, cloud service or network connection. It remains experimental.

The demo is **deliberately contrived** to contain a spectacular option price
jump. Its result is **not empirical evidence** that the strategy works. It is
a plumbing / testing fixture. The generated source file name explicitly says
`SYNTHETIC`.

## Quote file schema

Every row must represent an actual contemporaneous snapshot of a particular
option contract, ideally captured each minute or more frequently from a lawful,
licensed source. The `timestamp` must have a timezone offset, e.g.
`2026-09-22T10:00:00+05:30`.

| Column | Meaning |
|---|---|
| timestamp | ISO 8601 time, **with** UTC offset |
| contract | Unique stable ID including underlying/strike/expiry/right |
| right | CE or PE |
| expiry | Expiration date `YYYY-MM-DD` |
| underlying | Price of the underlying at the snapshot |
| bid, ask | Quoted rupees **per underlying unit** (NOT lot total) |
| bid_lots, ask_lots | Displayed quantity on the best side, in **whole lots** |
| volume | Volume traded during the current snapshot interval (NOT cumulative) |
| iv | Option implied volatility, fractional e.g. `0.25` means 25% |
| lot_size | Units of underlying exposure in one option lot |
| event_public | 1 iff a relevant event was already public by the timestamp |

**Do not** silently substitute daily OHLC option candles for timestamped quotes.
An option's daily high cannot be assumed attainable at the time we need to
sell. Do not use volume/OI/features that were not published at the exact time.
If your feed does not provide best bid/ask sizes, leave this strategy disabled
rather than manufacture them.

## What the existing swarm actually does

The baseline consists of independent rule-based scouts:

1. Directional acceleration: current underlying movement favors CE or PE.
2. Volume shock: recent option volume rises relative to earlier intervals.
3. IV repricing: option implied volatility accelerates.
4. Option momentum: actual quote midpoint begins rising with direction.
5. Public event: optional, **only** if recorded before the signal timestamp.

Three votes are required by default, with configurable eligibility, spread and
premium restrictions. These are **not** AI-calibrated probabilities or proven
signals. They should be replaced or augmented by trained models ONLY after
constructing timestamp-correct datasets with sufficiently many actual events.

Each qualifying contract can produce one simulated entry per day, at the
**next** snapshot's ask (never the signaling snapshot). The strategy purchases
only long options, with whole-lot sizing and limits on the total premium at
risk, plus an estimate of an entry fee. A future bid quote with sufficient
size is required for simulated exit. The deterministic exit rule stops out at
35% of entry ask, follows the peak after a 4x move with a 50% retracement,
or closes on timeout / last valid replay quote. These thresholds are
**unvalidated** configuration values and can be changed.

Results report (1) realized multiple before fees and (2) the hindsight best
bid multiple visible up to the holding horizon. The hindsight number does
**not** represent an executable exit policy. Quotes can be stale or vanish.

## Critical shortcomings before considering live trading

- No actual trained predictive AI model, no genuine edge validation and no
  statistical confidence calibration yet. A result from the demo means nothing.
- No walk-forward, purged cross-validation, embargo, multiple-testing
  correction, or truly out-of-sample evaluation has been implemented.
- No intraminute limit-order queue, actual fills, hidden liquidity, delay,
  circuit restrictions, market impact or broker exchange-rate verification.
- Snapshot-based best-bid/ask execution is optimistic even with shown sizes.
- Broker-specific brokerage, SEBI fees, taxes, GST, STT, strike/expiry
  treatment, exchange charges and position/contract size validation still
  require a current contract-aware fee model before real-life evaluation.
- No live broker connection, order management, API keys or credentials.
- No operational risk controls beyond coarse capital allocation in offline
  replay; no recovery after outages, open-position reconciliation or controls
  for exercise/expiry, tax lots and late-day liquidity.
- Candidate selection, spread filters and chosen thresholds must be
  evaluated as a FULL SYSTEM on untouched dates before accepting conclusions.
- Replaying multiple contracts assumes static account equity and independently
  allocated daily premium, not simultaneous correlated exits or varying cash.
- Fees only include configurable flat per-order amount and optional basis
  points, so realized P&L is NOT true post-tax P&L.

## Deployment plan (research first)

1. Acquire licensed, point-in-time intraday option L1 quotes and underlying
   observations. Verify vendor permissions for local archival and research.
2. Build a converter to this exact schema; enforce exchange time sync, contract
   IDs, split/expiry semantics and robust depth/quote quality validation.
3. Train XGBoost/LightGBM or calibrated probabilistic models on *earlier*
   dates only to estimate probabilities of very large future **executable**
   bid multiples. Include a `no trade` outcome. Avoid fitting on oracle MFE
   and then judging on the same date universe.
4. Compare trained model vs every scout and random/liquidity-matched baselines
   with walk-forward and fully independent final test periods. Stratify by
   liquidity, regime and expiry, and include all proposed positions.
5. Explicitly measure how many seemingly 100x opportunities vanish under
   realistic latency, bid depth, spread, taxes and fees.
6. Forward-test with no capital, then request human review and regulatory
   confirmation before any potential production execution integration.

## Mathematical account distinction

If a long option position consumes **0.25%** of the account and returns
**100x** on that particular premium investment, an idealized before-cost
account gain is `(100-1)*0.0025 = 24.75%` (not 100x on the whole account).
Usually, the trade may instead lose 100% of its premium. The system should
not ever auto-raise risk limits to chase a target multiplier.

## Cost

The prototype itself is free and entirely local. A real service requires
commercially suitable historical option quote data, possible market-data
subscriptions, reliable storage, broker charges and possibly a fixed-IP VPS.
Quote an actual provider before budgeting for years of second-level data.

## Scope / safety

No code in this package accesses the Internet. No buy/sell orders, options
orders, API credentials, exchange connections, or remote LLM calls exist. It
cannot lose money by accidentally executing a trade. This is research software
and is not financial advice.
