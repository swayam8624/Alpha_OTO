# Next milestone: first actual public-market research

**No real-money orders, paid subscriptions, cloud LLMs, or API credentials.**

## Update the inner repository on your Mac

The terminal output indicates that you made an outer \`Alpha_OTO\` directory,
then cloned a second, inner \`Alpha_OTO\` directory. Run:

\`\`\`bash
cd "$HOME/Desktop/All Might/Alpha_OTO/Alpha_OTO"
setopt interactivecomments
git pull --ff-only
PYTHONPATH=src python3 -m unittest discover -s tests -v
\`\`\`

## Collect completed public Coinbase Exchange candles

The collector aligns the request to the start of a completed candle, handles
small API bursts conservatively, and writes a companion SHA-256/data audit
manifest. This is a **research feed**, not authorized India-based live trading
access. Confirm the provider's data licence before commercial training or
retention.

\`\`\`bash
PYTHONPATH=src python3 -m alpha_oto fetch-coinbase --product BTC-USD --days 365 --granularity 3600 --out private_data/BTC-USD_1h.csv
PYTHONPATH=src python3 -m alpha_oto fetch-coinbase --product ETH-USD --days 365 --granularity 3600 --out private_data/ETH-USD_1h.csv
\`\`\`

Coinbase documents a 300-candle request cap and warns historical data can omit
intervals where no trades occurred. The collector pages in 299-candle windows
and the audit flags gaps, incomplete bars and unaligned timestamps.

## Audit and independently compare BTC and ETH

\`\`\`bash
PYTHONPATH=src python3 -m alpha_oto audit-data --csv private_data/BTC-USD_1h.csv --interval-seconds 3600 --out artifacts/BTC_audit.json
PYTHONPATH=src python3 -m alpha_oto audit-data --csv private_data/ETH-USD_1h.csv --interval-seconds 3600 --out artifacts/ETH_audit.json
PYTHONPATH=src python3 -m alpha_oto tournament --csv private_data/BTC-USD_1h.csv --out artifacts/BTC_tournament.json
PYTHONPATH=src python3 -m alpha_oto tournament --csv private_data/ETH-USD_1h.csv --out artifacts/ETH_tournament.json
\`\`\`

A separate longer-horizon daily experiment is also available:

\`\`\`bash
PYTHONPATH=src python3 -m alpha_oto fetch-coinbase --product BTC-USD --days 1095 --granularity 86400 --out private_data/BTC-USD_1d.csv
PYTHONPATH=src python3 -m alpha_oto tournament --csv private_data/BTC-USD_1d.csv --out artifacts/BTC_daily_tournament.json
\`\`\`

**A null selected agent is not a crash or permission to weaken the gates.**
The tournament selects on its validation partition and reports an additional
test partition once. Reusing that test partition for repeated tuning destroys
its out-of-sample interpretation. Hourly candles are serially correlated;
8,760 hours do not mean 8,760 independent trades.

## Local ML separately

\`\`\`bash
PYTHONPATH=src python3 -m alpha_oto train-local --csv private_data/BTC-USD_1h.csv --out artifacts/BTC_local_model.json
\`\`\`

This command uses ALL supplied bars to create a research artifact; do not
claim any of those bars are unseen for that saved model. The tournament trains
a separate model only on its training partition.

## Review outputs

Review \`artifacts/BTC_tournament.json\`, \`artifacts/ETH_tournament.json\`,
the corresponding audit reports and the \`*.csv.manifest.json\` files.
A passing data audit checks narrow technical properties only. The present
backtester does not model full market microstructure, tax obligations, actual
bid-ask queues or legally permitted broker execution. **No strategy is
authorized for live trading by these commands.**
