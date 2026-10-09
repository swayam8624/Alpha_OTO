# Alpha_OTO ML Laboratory v0.3 — macOS Apple Silicon

**Research only; no orders or broker integration.** The lab uses only local computation.
No API keys, cloud inference, accounts or paid datasets are required for this milestone.

## Background and interpretation of the previous results

On ~8,750 real BTC-USD and ETH-USD hourly candles, the original five-agent
validation tournament selected no candidate. Each dataset had exactly ten
missing hourly intervals, and the original hand-crafted agents lost money
net of the assumed trading costs. This is evidence *against* deploying those
particular strategies with those assumptions, not a runtime bug.

The new laboratory tests different model families and multiple label horizons.
It **does not lower the original selection bar to manufacture a winner**. It
saves a research model even if no strategy qualifies, because model artifacts
can still be studied and compared. Saved artifacts are never live-approved.

## 0. Installation (your local terminal)

From inside the cloned Git repository (the inner `Alpha_OTO` directory):

```bash
cd "$HOME/Desktop/All Might/Alpha_OTO/Alpha_OTO"
setopt interactivecomments

git pull --ff-only
bash scripts/bootstrap_mac.sh
source .venv/bin/activate
```

This installs free wheels for NumPy, SciPy, scikit-learn and joblib. If Python
3.13 produces incompatible wheels, consider a supported Python 3.12/3.13
from Homebrew or python.org and rerun using `PYTHON_BIN=python3.12`.

Optional advanced boosted-tree implementations:

```bash
source .venv/bin/activate
python -m pip install lightgbm xgboost
```

On Apple Silicon `lightgbm` may require OpenMP; install it using
`brew install libomp` if importing LightGBM fails. This is free open-source
software. Neither booster is necessary to use the default baseline lab.

## 1. Historical market data: public crypto candles

Existing data are reused by `run_crypto_research.sh` if files are present.
Otherwise, it fetches 365 days of BTC/USD and ETH/USD one-hour OHLCV from the
public Coinbase Exchange endpoint. Obtain and retain data only within the
provider's applicable terms and permissions.

```bash
bash scripts/run_crypto_research.sh
```

The script audits each dataset; **a dataset with gaps remains flagged**. Our
model excludes *all samples whose 49-bar feature window OR forward return label
crosses any missing interval*. Missing prices are never silently filled.
The research intentionally has less available training data near those gaps.

To explicitly ask Coinbase for only the missing candles and write a SECOND
CSV, retaining the original dataset unchanged:

```bash
alpha-oto repair-coinbase --csv private_data/BTC-USD_1h.csv
alpha-oto repair-coinbase --csv private_data/ETH-USD_1h.csv
```

The repaired outputs have `_repaired.csv` suffix and a companion `.repair.json`
quality report. The report contains `repair_status`, `recovered_intervals`,
`unresolved_intervals`, and exact unresolved UTC timestamps. An empty Coinbase
response is recorded as an unresolved gap instead of crashing. Actual API
errors or conflicting historical prices still fail rather than silently masking
problems. If missing candles are unavailable from the provider, **do not invent
them**. The ML lab excludes impacted windows from either CSV.

To use enhanced repaired datasets, explicitly run the next section with the
repaired path instead of the original path, and note this as a new research
experiment. Do **not** repeatedly tune against the same holdout period.

## 2. Manually specify training experiments

```bash
alpha-oto ml-research \
  --csv private_data/BTC-USD_1h.csv \
  --out artifacts/ml/BTC \
  --models logistic,histgb,forest,lightgbm,xgboost \
  --horizons 1,4,12 \
  --interval-seconds 3600 \
  --side-cost-bps 25 \
  --position-fraction 0.25

alpha-oto ml-research \
  --csv private_data/ETH-USD_1h.csv \
  --out artifacts/ml/ETH \
  --models logistic,histgb,forest,lightgbm,xgboost \
  --horizons 1,4,12 \
  --interval-seconds 3600 \
  --side-cost-bps 25 \
  --position-fraction 0.25

python scripts/summarize_research.py \
  artifacts/ml/BTC/research_report.json artifacts/ml/ETH/research_report.json
```

The default automation script uses the three reliably installed scikit-learn
models and horizons 1/4/12. With extra boosters installed, rerun the script:

```bash
ALPHA_MODELS=logistic,histgb,forest,lightgbm,xgboost bash scripts/run_crypto_research.sh
```

Do not use GPU/cloud services unless measured training time warrants them.

## 3. How the data, labels, and training work

Each record is identified by a candle starting at timestamp `t`. Features are
computed from 49 **completed candles** through `t`. Because the bar is not
complete until its end, a prospective signal is assumed to enter at the next
candle's opening price, at `t+1` in hourly notation. This is a simplified
best-case fill timing assumption, not an actual execution guarantee.

Training label for a horizon of H bars:

```
net_return = open[t+1+H] * (1 - side_cost)
             / (open[t+1] * (1 + side_cost)) - 1
positive_label = int(net_return > 0)
```

The default side cost is **25 basis points for each side** (50 bps simple
round-trip approximation including fees and slippage). Real charges depend on
market and actual orders; our cost model is hypothetical and **does not include
India's VDA tax consequences**. It must not be confused with expected earnings.

Feature vectors include lagged returns, realized volatilities, candle
body/range, volume changes, RSI, moving-average ratios, breakouts and relative
price levels. All numeric features are constructed locally from OHLCV.

The chronological boundaries are:

```
0% ---------------- 44% --- 56% --- 68% --- 80% ----------- 100%
| fold 1 training | fold 1 validation  |                  |
|     fold 2 training     | fold 2 val |                  |
|           fold 3 training          | fold 3 val |       |
|               final training 0..80% (purged)   | holdout |
```

A training row is rejected if its label exit occurs at or beyond the next
validation boundary. This *purge/embargo* prevents target leakage. Models are
trained separately in each fold; feature standardization is learned using
fold training data only. A single final model is refit using data whose target
is fully known before the final test begins. Selection sees the first 80%
only, never held-out labels.

The selected research configuration is the one with highest predeclared
validation score. `shadow_candidate_only` is true only when it has positive
validation performance, enough trades and positive results in 2+ independent
validation windows. **Even shadow-candidate status never authorizes trading.**

The last 20% is read once for an initial diagnostic: after inspecting those
results, tuning against the same slice makes it *no longer untouched*. To
measure repeatability we will need genuinely new future data, not parameter
search over the same year.

## 4. Output files and how to interpret them

Per asset, the lab saves:

- `research_report.json`: every candidate's fold-level stats, final selected
  configuration, held-out return, baseline, Brier score, AUC and gap counts.
- `research_model.joblib`: locally fitted scikit-learn/optional booster model;
  **not** an order-execution object.
- `model_manifest.json`: data SHA-256, model SHA-256, training cutoff, feature
  schema, inferred horizon and explicit research-only safety designation.

`joblib` uses pickle internally; never load third-party model files unless you
trust their origin. `ml-predict` refuses bars already present in training or
holdout research; fetch newer completed candles for any subsequent inference.

**The output is not a validated profit probability.** Classification scores
can be poorly calibrated. The realized-only drawdown in this inexpensive
simulation is a lower bound on actual risk, not a trustworthy maximum drawdown
forecast. Missing order book, spread, slippage, liquidity, tax, funding and
settlement effects must be resolved before any live operation.

## 5. Optional entirely local Ollama report writer

The trading ML models train **without an LLM**. If you want a prose explanation
of one experiment, install Ollama for macOS, start its local daemon, and run:

```bash
ollama pull qwen3:4b
alpha-oto local-report --json artifacts/ml/BTC/research_report.json --model qwen3:4b
```

This downloads ~2.5 GB of model weights and runs locally, not through paid
cloud inference. If `ollama` is already installed, no other installation is
needed. The localhost reporter does not train trading models or place orders.

## 6. Next research gates

We still need several years of different market regimes, a separate
forward-only paper period, tax-aware portfolio accounting for Indian residents,
proper fill/quote history, point-in-time adjusted stock data with corporate
actions, and independent evidence that adaptive strategy selection improves
upon buy-and-hold or a simpler portfolio. No order-placement API has been
implemented. A single successful holdout result is not permission to trade.
