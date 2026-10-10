# Alpha_OTO — Local-first global quantitative research

> **Current state:** research foundation, no broker integrations, no live orders, no
> profitability claims. The project intentionally cannot buy or sell assets.

A multi-market research system designed to make hypotheses reproducible before
any money or paid infrastructure is needed. It loads strict point-in-time OHLCV
files, downloads *optional* public Coinbase Exchange spot candles, simulates
next-open executions with costs, locally trains a logistic ML model, compares
agents chronologically, and calculates a profit-funded infrastructure budget.

## What works today

| Feature | Implementation | State |
|---|---|---|
| OHLCV CSV import/export and validation | Strict UTC-offset timestamps, price integrity, chronological ordering | Implemented |
| Public cryptocurrency research data | Read-only Coinbase Exchange candle API, rate/size bounded | Implemented (requires internet) |
| Trend, mean-reversion and breakout agents | Transparent, price-only historical strategies | Implemented, unvalidated |
| Custom locally trained ML | Standard-library logistic classifier, train-only normalization | Implemented, uncalibrated |
| Trading friction | Next-bar-open execution, adverse slippage, fees, no leverage | Implemented for simplified OHLC replay |
| Chronological tournament | 60% training, 20% validation selection, 20% untouched report | Implemented, not proof of alpha |
| Portfolio safeguards | Per-strategy position cap and drawdown halt | Simplified backtest only |
| Local optional LLM | Explicit Ollama localhost report, **never** trade decisions | Implemented (optional) |
| Reinvestment policy | Only positive realized profit after cost/tax reserve, subject to cash floor | Implemented (planning only) |
| Multi-venue live execution | Broker, exchange, orders, authentication | **Not built** |
| Licensed historical options quotes | Depth, ticks, microstructure and verification | **Not acquired** |

## Quick start (Mac/Linux/Windows, Python 3.11+)

```bash
# The core uses ONLY Python's standard library; no subscriptions or API keys.
python3 -m pip install -e .
python3 -m unittest discover -s tests -v

# Research using a CSV with timestamp,symbol,open,high,low,close,volume:
alpha-oto simulate --csv path/to/own_ohlcv.csv
alpha-oto tournament --csv path/to/own_ohlcv.csv

# Optional public read-only candles (requires internet, check Coinbase terms):
alpha-oto fetch-coinbase --product BTC-USD --days 14 --granularity 3600 --out private_data/btc_hourly.csv
alpha-oto tournament --csv private_data/btc_hourly.csv

# Compute a HUMAN-APPROVAL-ONLY hardware reinvestment allowance:
alpha-oto reinvest --realized-profit 25000 --tax-reserve 5000 --liquid-cash 80000 --cash-floor 60000 --rate 0.20

# Optional: after installing Ollama locally AND fetching a model yourself:
alpha-oto local-report --json artifacts/tournament.json --model qwen2.5:3b
```

When using local Ollama, user must install Ollama and download model weights on
their own machine. It is optional and **never** consulted for live orders. No
cloud AI service, GitHub secret, or brokerage credential is needed.

## Evidence rules

- Every strategy observes **only prior completed candles**; fill is delayed to
  the next candle opening price with modeled adverse slippage and fees.
- ML target uses the next opening-to-following-opening return, with training
  labels fully contained inside the training partition. Train-only means/scales.
- Agent selection uses validation, never test. Test is reported once as a
  diagnostic; repeated tuning on test invalidates the 'untouched' claim.
- OHLC candles cannot establish actual option fillability or order-book queues.
- Annualized Sharpe depends on dataset frequency; defaults to **daily bars**.
  For hourly bars pass an appropriate annualization parameter via Python.
- Taxes, spread, DP charges, financing, gaps, calendars, corporate actions,
  withdrawals and settlement rules are **NOT** fully modeled. These backtests
  cannot justify real trading without substantial additional validation.
- **No investment returns are promised.** A strategy can lose all deployed
  capital. The system never assumes profitability every second.

## First checkpoint requiring user action

1. Run on your own machine or install Python/optional Ollama. This workspace can
   execute the tests and create the repository before that checkpoint.
2. Select data products, confirm storage and licensing rights for intended use,
   and approve any paid historical tick/quote access before a detailed options model.
3. To pursue eventual live trading, choose eligible broker accounts, review
   regulatory obligations for Indian residents, and supply credentials **locally**.
   Never commit credentials, PII or trade secrets to this repository.

See [architecture](docs/ARCHITECTURE.md) and [cost/reinvestment plan](docs/COST_AND_GATES.md).

## Next milestone: actual public-market historical research (v0.3)

The previous synthetic tournament returning `selected: null` is a correct,
conservative outcome—not an exception. **Do not deploy the synthetic model.**
The repository now includes an offline quality audit and source-provenance
manifest to make the next real-data exercise inspectable.

Follow [the Mac real-data walkthrough](docs/NEXT_REAL_DATA.md). No API key,
subscription, or cloud LLM is required. The public Coinbase feed is for
**research** and is NOT a statement that Coinbase trading is legally available
to Indian residents or that this feed grants commercial training rights.

## New: locally trained multi-model quantitative research (v0.3)

See **[the complete macOS training, data and optional Ollama guide](docs/ML_RESEARCH.md)**.

```bash
# No subscriptions, API keys or broker credentials; installs free local ML dependencies.
bash scripts/bootstrap_mac.sh
source .venv/bin/activate

# Reuses existing BTC/ETH files or downloads free public data if absent,
# audits timestamps, trains three local model families across 1h/4h/12h
# prediction horizons and three chronological validation windows.
bash scripts/run_crypto_research.sh

# Optional LightGBM/XGBoost local training:
python -m pip install lightgbm xgboost
ALPHA_MODELS=logistic,histgb,forest,lightgbm,xgboost bash scripts/run_crypto_research.sh
```

**Research-only**: includes gap-aware feature construction, purged targets,
held-out validation, cost-aware simulated trades, serialized local models,
manifest checksums and independent buy/hold comparisons. No live orders,
no guaranteed performance and no cloud AI usage.

## New v0.5: Precommitted forward-only research evidence

Omega's previously inspected breakout holdout **lost 1.95% versus a +18.22%
benchmark return**. The historical holdout is no longer unseen. Instead of
continuing to optimize against it, the new [forward-evidence protocol](docs/FORWARD_EVIDENCE.md)
freezes challenger parameters and writes a timestamped, tamper-evident local
journal of **future** research intents. Missing market data block paper fills;
no broker is connected, and actual bid/ask executable profitability has not
been established. The script `scripts/forward_once.sh` can be invoked manually
on your Mac; it only fetches recent read-only public history and updates a
**paper-only** ledger, never buys or sells assets. No cloud LLM is required.


## v0.6 — Broker emulator, crash-safe accounting and fail-closed risk core

The [production-core specification](docs/PRODUCTION_CORE.md) documents the new
SQLite event/financial ledger, broker emulator, order lifecycle, conservative
risk gate, independent broker-vs-local reconciliation, and read-only health tool.
All production-core commands are **simulated only**, with no live API calls:

```bash
python -m alpha_oto.production demo --db artifacts/production/simulation.sqlite3
python -m alpha_oto.production status --db artifacts/production/simulation.sqlite3
python -m alpha_oto.production health --db artifacts/production/simulation.sqlite3
```

A simulated broker that can withstand unit-test fault injection is **not** a
licensed production broker implementation or an economically validated alpha.
No real order execution, no live trading permissions, and no trading revenue
are introduced in v0.6.

## v0.7: Durably simulated broker — restart and recovery tests

The [v0.7 durable simulated broker runbook](docs/DURABLE_BROKER_V07.md) adds
an **offline, separate SQLite remote emulator**, independent from the existing
crash-consistent order/accounting ledger. It can persist remote order acceptance
before throwing a simulated lost acknowledgment, survive actual Python process
restarts, import partial fills exactly once, and detect divergent financial
records. No live broker credentials, external order endpoints or live permissions
were introduced. Use `python -m alpha_oto.production durable-start`, then
`durable-recover`, `durable-complete`, and `durable-status` with the SAME two
`--db` and `--broker-db` paths. Do not reuse existing demo database filenames.

## v0.8 — Process-isolated simulated risk and sequenced bid/ask feed

See [v0.8 risk and quote-feed engineering manual](docs/ISOLATED_RISK_V08.md).
The new code uses a strict, version-pinned market calendar/instrument whitelist,
SQLite WAL event-chain quote ingestion with fail-closed sequence quarantine,
Unix-socket simulation risk authorization, and a two-process synthetic
broker fault-injection demonstration. It includes **no** real broker interface,
no live executable market feed, and no evidence of profitable strategies.

```bash
PYTHONPATH=src python scripts/risk_isolated_smoke.py \
  --outdir "artifacts/production/isolation_$(date +%Y%m%d_%H%M%S)"
```

No new paid service, cloud model, or real trading capital is required.

## v0.9 — Coinbase public Level 2 quotes, recorded replay and watchdog

The [v0.9 market-data and operations guide](docs/REALTIME_MARKET_DATA_V09.md)
adds a **read-only** Coinbase Exchange Level2 WebSocket client (optional free
`websockets` dependency), an absolute-size order-book reconstruction engine,
verified timestamped best-bid/ask publication, tamper-evident raw event logs,
durable disconnect quarantines, and an independent risk daemon health monitor.
It also adds a separate-process fake-price end-to-end scenario (no capital):

```bash
PYTHONPATH=src python scripts/l2_isolated_smoke.py \
  --outdir "artifacts/production/l2_$(date +%Y%m%d_%H%M%S)"
```

Capture is **read-only public market data**, not proof of a profitable edge,
licensed commercial feeds, account eligibility or actual order fills. The feed
is sealed at the end of every bounded capture. No live brokerage authorization
or private key usage is included.

### v0.9.1: Public Level 2 WebSocket compatibility

The old Exchange `level2` channel rejects unauthenticated subscriptions.
`python -m alpha_oto.production.marketd capture` now uses Coinbase Advanced
Trade's **public** read-only L2 feed, including connection-wide sequence
verification and fail-closed quarantine. Use fresh SQLite and journal paths.
No API key, broker account or live order capability is introduced. See
[market-data protocol notes](docs/REALTIME_MARKET_DATA_V09.md).
