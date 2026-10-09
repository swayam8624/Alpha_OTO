# ATLAS/Alpha_OTO Omega — Quantitative Research Engine v0.4

**Scope:** research, offline simulation, offline model training and read-only market-data acquisition. The repository has **no live broker order endpoint**, no brokerage credentials, no real-money allocation, and no cloud AI dependency. Avoid conflating a working backtester with a working profitable strategy. Every current dataset is research evidence only.

## 1. What is actually built

The original single-asset ML laboratory remains supported. The new engine adds a multi-asset next-open portfolio simulator, 9 deterministic quant agents across 6 families, cost/volume-sensitive portfolio rebalancing, covariance-shrunk risk-parity allocation, volatility/drawdown regime throttles, multi-fund chronological validation, risk and execution stress tests, a local pooled cross-asset return model, moving-block bootstrap diagnostics, and a separately labeled **non-executable short-leg statistical-pairs experiment**.

| File | Responsibility |
|---|---|
| `quant_agents.py` | Trend, momentum, mean reversion, breakout, volatility-scaled momentum, defensive filters and long-only allocation |
| `covariance.py` | Shrinkage covariance, variance estimation, capped risk budgeting |
| `regimes.py` | No-lookahead volatility shock and collective drawdown exposure throttling |
| `portfolio.py` | Unified same-currency, same-frequency, 24/7 spot portfolio ledger, cash, fees, slippage, bounded candle-volume participation, stale data rejection, trailing max drawdown risk halt |
| `quant_validation.py` | Predeclared 3-fold disjoint validation, no tuned holdout unless a candidate passes validation; moving-block uncertainty |
| `cross_asset_ml.py` | Local pooled regression with own-market 23 features + 3 *other-market* features; expanding train folds, purged forward labels, selected model checked on held-out period once |
| `quant_stress.py` | No-lookahead performance under higher fees, adverse slippage, reduced liquidity, reduced exposures; comparative reports |
| `pairs_lab.py` | Fixed-period log-price hedge regression, past-only spread Z-score, hypothetical two-leg long/short simulator with fees and borrow cost, no claim of cointegration |

All operations are local CPU work. No GPU or LLM is needed. LightGBM/XGBoost are optional; they do not receive capital or install broker integrations.

## 2. Asset and market scope

**This release simulates only aligned 24/7 historical candle datasets in the same quote currency** (e.g., `BTC-USD`, `ETH-USD`). The system rejects ambiguous or differing quote currencies. That prevents treating US dollars, Indian rupees and USDT as interchangeable cash. NSE/US equities use different sessions, holidays, taxation, settlement, corporate actions and borrowing permissions, so **do not paste those CSVs into the same 24/7 simulator**. Those will require a proper exchange-calendar and multi-currency ledger in a future release.

Coinbase historical candles can contain absent hours, and your two datasets have ten missing hourly intervals, which remain absent after public API repair. Omega computes a complete common hourly UTC timeline and **never manufactures absent prices**. Gapped candles reset price-feature history for the affected asset, block new orders on incomplete ticks and mark portfolio valuations as stale if the asset was held. At a gap return, the simulation recognizes price discontinuity; no signal based on the missing bars is eligible. Missing price periods can still make mark-to-market drawdowns understate true intraperiod risk.

The two BTC/ETH files on your machine are not in GitHub; data files are ignored by `.gitignore`. No confidential datasets need to be uploaded for a local run.

## 3. Simulation accounting

Each signal is computed using **only completed bars through t−1** and is simulated as an order on bar **t at open**. This assumes a best-case zero-latency close-to-next-open boundary. Real orders may miss that price. For fee/slippage assumptions `f` and `s` basis points per side, a buy at open `O` is filled at `O(1+s/10000)` and pays `notional*f/10000`. Sells receive `O(1−s/10000)` minus `f`. Each order is additionally capped by `max_participation × PREVIOUS COMPLETED BAR volume × current open`. Using the current bar's total volume would leak future information into the opening fill, and is explicitly forbidden in this implementation. Previous-bar volume is only a rough capacity proxy and does NOT prove an order is executable at the opening price. Live execution still requires pre-trade bid/ask quotes, market depth and venue-specific sizing.

Cash never goes negative beyond numerical tolerance, positions are long-only, and total target notional cannot exceed the approved gross budget. Initial cash is independent for every fold, and strategies and benchmarks go through the same code. Current `final_equity` subtracts an **estimated, not executed, terminal exit cost**; `mark_to_market_equity` and `estimated_final_exit_cost` are reported separately. P&L remains an estimate, NOT real fills.

**Drawdowns** are based on completed candle close valuations, not intrabar highs/lows; during missing data, last known prices are informational stale marks. This may understate risk. A close-to-close drawdown threshold prevents new buys and instructs the simulator to attempt to reduce existing exposure at the next fresh execution window, limited by participation caps. There is no guaranteed loss limit, stop fill or trading obligation. No market impact, exchange halt, tax-lot method, crypto VDA tax, financing/margin, order-book queue or multi-currency settlement is modeled.

## 4. Quant agents and risk allocation

Each agent produces a nonnegative, non-calibrated score from historical returns and volatility. Its score is **not** a probability of profit. The allocator independently applies gross and per-asset caps, optional inverse-volatility or shrinkage-covariance risk budgeting, and a portfolio volatility limit. A `regime_control` filter may only REDUCE exposure under elevated realized volatility or drawdown; it can never increase hard limits. The portfolio can correctly decide to hold cash.

The covariance estimator is a sample estimator with off-diagonal shrinkage; risk-parity allocation uses bounded multiplicative iterations, so it is not guaranteed globally optimal and is sensitive to nonstationarity. This is intentionally less dangerous than an unconstrained optimizer that concentrates the entire account on attractive-looking noisy forecasts.

The default `quant-tournament` tests 9 fixed candidate configs in 3 chronological validation periods (44–56%, 56–68%, 68–80%). It does not train these rule-based agents. It promotes a *shadow research hypothesis only* if at least 2 of 3 periods outperform an equally risk-budgeted reference, its penalty-adjusted score is positive and enough fills are observed. It evaluates the last 20% only if these conditions hold. **No strategy is live-approved.** Its use of repeated candidate selection creates multiple-testing bias not eliminated by the holdout.

## 5. Train real cross-market AI locally

Cross-market regression pools synchronized history from two or more assets. Each training example includes the original 23 price/volatility/volume features plus three own-vs-other-movement features based only on completed bars: mean other-asset one-bar return, mean other-asset 24-bar return and the own 24-bar return minus that second market summary. Candidate models are `ridge`, `histgb`, optional `lightgbm`, optional `xgboost`; horizons are default 4 and 12 completed bars.

At the end of bar t, we predict the (cost-adjusted) log return from next bar t+1 open to t+H+1 open. Train samples are included **only when their full forward outcome completes before the training boundary**; validation data are never used to fit fold models. Three training windows expand to starts of 50%, 60% and 70% of the shared timeline; disjoint validations end at 60%, 70% and 80%. An end model, if it meets the predeclared shadow criteria, is refit using data whose label ends before 80%; the final 20% is examined **once**. Unseen future data must be collected before any further meaningful out-of-sample claim.

`model_manifest.json` from the previous single-asset pipeline remains its own independent experiment. **These are experimental regressors, not calibrated success probabilities.** A large predicted net return in one market is a hypothesis, not evidence of a certain order fill. The same asset-specific forward cost model must be reviewed before any deployment.

## 6. Statistical arbitrage lab

`pairs-research` is a **hypothetical long/short diagnostics experiment**. OLS fits `log(Y) = alpha + beta log(X)` from TRAIN data only. The residual spread is standardized against prior data only. The simulator accounts for fees and a simplified short borrow rate, but it cannot establish borrow availability, margin funding, venue permission, or actionable order-book liquidity. Regression of two prices is NOT proof of cointegration; this release explicitly reports `cointegration_verified=false` and does not use the holdout to promote the pair. It is never eligible for broker execution.

Pairs are strictly synchronized; without the optional `--longest-contiguous-segment`, an observed gap blocks the experiment. The explicit flag selects the largest fully recorded period, reports excluded time, and does not interpolate missing prices. Use the output for research comparisons, not a real short sale. Indian residents must separately confirm instrument permissions with the broker and legal/tax specialists.

## 7. Execute local end-to-end research on your Mac

Your previously installed M2 Pro scientific stack is adequate. In your **inner cloned Git repository**:

```bash
cd "$HOME/Desktop/All Might/Alpha_OTO/Alpha_OTO"
source .venv/bin/activate
git pull --ff-only
python -m unittest discover -s tests -v

# One command: reuse local BTC/ETH, or download public candles if absent;
# run portfolio quant tournament, cost/regime stress, cross-asset ML,
# separate hypothetical pairs diagnostic, and objective summary.
bash scripts/run_omega_research.sh
```

To run individually:

```bash
alpha-oto quant-portfolio --csv private_data/BTC-USD_1h.csv \
  --csv private_data/ETH-USD_1h.csv --agent momentum --fast 48 --slow 168

alpha-oto quant-tournament --csv private_data/BTC-USD_1h.csv \
  --csv private_data/ETH-USD_1h.csv

alpha-oto cross-ml --csv private_data/BTC-USD_1h.csv \
  --csv private_data/ETH-USD_1h.csv --models ridge,histgb \
  --horizons 4,12 --out artifacts/omega/cross_ml

alpha-oto quant-stress --csv private_data/BTC-USD_1h.csv \
  --csv private_data/ETH-USD_1h.csv

alpha-oto pairs-research --csv private_data/BTC-USD_1h.csv \
  --csv private_data/ETH-USD_1h.csv --longest-contiguous-segment
```

Optional faster-gradient models on your existing installation (no cloud):

```bash
python -m pip install lightgbm xgboost
alpha-oto cross-ml --csv private_data/BTC-USD_1h.csv \
  --csv private_data/ETH-USD_1h.csv \
  --models ridge,histgb,lightgbm,xgboost --horizons 4,12 \
  --out artifacts/omega/cross_ml_boosters
```

**No additional Ollama model is needed.** Your `qwen3:4b` is suitable for optional localhost-only research explanations using `alpha-oto local-report --json artifacts/omega/quant_tournament.json --model qwen3:4b`. Ollama should not be used for arithmetic, profitability declarations or order authorization; all model selection numbers come from audited Python code.

## 8. What to inspect and what not to claim

Output folder `artifacts/omega/` contains `quant_tournament.json`, `stress.json`, `cross_ml/cross_asset_research.json`, and `pairs_research.json`. Reports include explicit shadow-only or non-executable designations. Running the same full research suite multiple times against the same last 20% does **not** create fresh holdout evidence. Record each iteration as exploratory, freeze strategies, then run an incremental forward observation period.

The simulator still needs live bid/ask archives, instrument lot sizing, point-in-time survivorship-free stock universes, exchange calendars, sector controls, separate cash pools for jurisdictions, permissions, tax-ledger accounting, actual latency, order reconciliation, infrastructure redundancy and regulatory clearance. A working model does not justify spending trading capital. Hardware reinvestment remains a manual decision based on **verified realized post-tax profits**, never paper P&L.

**Reproducibility checks:** `PYTHONPATH=src python -m unittest discover -s tests -v`; CI runs the same test suite with free local dependencies. A passing test suite establishes specified software invariants, not market edge.

## 9. Adaptive shadow committee (online learning, not live orders)

The `shadow-swarm` command executes an additional independent experiment using
nine expert strategies. At each scheduled rebalance, expert votes are computed
from **completed** bar data only. The committee updates log weights from the
previous vote's subsequent observed close-to-close proxy gain, subtracting a
predeclared turnover-change friction penalty. A tempered exponential-weights
rule updates how much confidence each expert receives; repeated sufficiently
negative cumulative evidence can quarantine an expert. Frozen feeds prevent
updates entirely, and chronological reversals are rejected. Quarantined experts
may rehabilitate after improved observed proxy evidence, but no amount of
online proxy utility grants live trading authority.

`shadow-swarm` compares a fixed learning-rate grid of 0.5, 2 and 6 across
three disjoint validation windows, uses the same cost-aware portfolio ledger
and equal-weight reference, and evaluates the last 20% only when a validation
candidate passes strict shadow criteria. The committee's proxy rewards are
*not broker profit and not account NAV*: only its separate portfolio replay
contains estimated fills, fees and marked cash. Repeatedly adjusting committee
rules after seeing the same holdout is invalid as a confirmation procedure.

```bash
alpha-oto shadow-swarm --csv private_data/BTC-USD_1h.csv \
  --csv private_data/ETH-USD_1h.csv \
  --out artifacts/omega/shadow_swarm.json
```

The full `scripts/run_omega_research.sh` includes this command. It does NOT
call Ollama or any cloud inference. This is a cost-aware **research comparator**,
not proof that adaptive selection beats a simple buy-and-hold allocation.
