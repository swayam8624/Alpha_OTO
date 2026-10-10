# Omega Forward Evidence Protocol v0.5

**Research only. The module does not connect to a broker, cannot place an order, and cannot guarantee profits.** This addition responds to October 10 research observations: rule-based breakout holdout return **−1.9495%** versus the risk-capped BTC/ETH equal-weight benchmark **+18.2171%** over that same holdout; the selected strategy's excess was **−20.1666 percentage points**. Pooled ML, the adaptive shadow swarm, and non-executable pairs research did not qualify. The previous holdout has already been inspected and must not be treated as unseen during future tuning.

## Why the old history is no longer a valid test of new hypotheses

After inspecting the 2025–26 dataset's final period, optimizing additional model families against that same period becomes retrospective selection. Running the simulation five thousand more times does not create five thousand independent observations. The program now has a *precommitment protocol*: freeze a named strategy list while no future observations exist, save a local append-only chain of decision events, and evaluate only candles whose opening time occurs strictly after the signed-off freeze. Hashes are tamper-evident for accidental edits but are **not cryptographic signatures**. Any edited prior candle invalidates the experiment; revised prices cannot silently rewrite prior reported results.

## Explicit signal timing (conservative by design)

When started at e.g. 10:20 UTC, signals may be prepared for 11:00 UTC using only candles already complete at 10:20. We do **not** use the 10:00–11:00 candle, which is still forming. The intent contains its UTC commit timestamp, target future open, required previous-bar volume, strategy-specific position weights, and a durable local journal record. The program **does not** simulate fills for historical candles with no earlier committed intent.

A later invocation can observe the completed 11:00–12:00 candle and *hypothetically* fill that precommitted 11:00 action at the reported candle open plus slippage. This is still an optimistic OHLC proxy, not an executable bid/ask fill or genuine broker paper-trading execution. A missing quote skips the intent, rather than inventing it. The ledger tracks cash, long-only holdings, fees, close-based NAV/drawdown, staleness and the equal-weight/cash comparators. There is no leverage, shorting, options, derivatives, margin or real trading permissions.

**You must invoke the pipeline frequently enough to precommit the NEXT OPEN.** Running it retrospectively once a week creates no simulated trades for the intervening hours; that is intentional. Cron/launchd can schedule it on an always-on machine, but the current implementation makes no claim of surviving laptop sleep, internet outages or operating reliably without an external watchdog. It refuses recent data that lack completed candles. To avoid spending money, use your existing MacBook first.

## Run it with the data already on your Mac

```bash
cd "$HOME/Desktop/All Might/Alpha_OTO/Alpha_OTO"
source .venv/bin/activate
git pull --ff-only
python -m unittest discover -s tests -v

# These are public RESEARCH prices only. No Coinbase account or API key used.
alpha-oto data-update-coinbase --csv private_data/BTC-USD_1h.csv
alpha-oto data-update-coinbase --csv private_data/ETH-USD_1h.csv

# Freeze predeclared challenger strategies and an equal-weight/cash reference.
# Executed ONCE: the state path must not already exist.
alpha-oto forward-freeze \
  --csv private_data/BTC-USD_1h.csv \
  --csv private_data/ETH-USD_1h.csv \
  --state private_data/forward/forward_experiment.json

# Run at the next opportunity (and preferably once per hour thereafter).
# The update script does not install a daemon or run by itself.
bash scripts/forward_once.sh

# Read local status and benchmark-adjusted research figures.
alpha-oto forward-status --state private_data/forward/forward_experiment.json
```

Before freezing, the input data must be complete through a recent market hour. The entry command refuses stale source files and refuses to overwrite any previous freeze state. If an input file is altered or an already processed forward bar is revised, the forward observer refuses to continue; investigate the historical discrepancy rather than replacing the evidence quietly.

A 60-day freeze that observes only a handful of valid precommitted intents is not a robust trading result. The early status reads `INSUFFICIENT_NEW_FORWARD_OBSERVATIONS` until at least 720 fully synchronized hourly bars have been recorded, and **reaching that number does not confer trading approval**. Commission, spread, crypto VDA tax, execution latency, funding, settlement and venue restrictions would still need independent validation for actual capital deployment.

## Append-only public-data updater

The separate `data-update-coinbase` operation fetches only the small period since the latest stored bar (maximum 299 candles), checks overlapping bars for revisions, and atomically appends observed new completed candles. If history has been offline for more than the supported window, the operation fails instead of silently skipping weeks. The original documented 10 Coinbase historical hourly gaps remain absent; they are never interpolated. A dataset sourced from Coinbase does **not** imply Indian residents can legally trade at that venue or that the venue licenses all possible downstream uses.

## Hardware and AI costs

The entire forward evidence protocol runs with the Python standard library and installed Omega source. Its API usage is public historical prices only and generates no cloud LLM bill. Your existing `qwen3:4b` can optionally summarize a research report, but it is not consulted to decide fills or classify profitability. No purchase or VPS setup is required.

## Known limitations

This is a simple long-only spot paper ledger with two demonstration agents and two controls, not a real order-book simulator. Passive benchmark paper positions can be marked to recent completed closes, but those are not brokerage holdings. A loss-triggered halt can only make a strategy target zero at a *future* eligible paper intent; it is not a guaranteed market stop-loss. Strategy parameters remain frozen and were not retuned to the previously observed holdout. For statistical evidence of alpha, accumulate diverse future conditions and compare portfolio-level outcomes after credible execution/tax frictions; do not substitute attractive historical charts for this verification.
