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
