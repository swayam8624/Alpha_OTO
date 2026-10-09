#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then source .venv/bin/activate; fi
mkdir -p private_data artifacts/ml
for ASSET in BTC ETH; do
    CSV="private_data/${ASSET}-USD_1h.csv"
    if [ ! -s "$CSV" ]; then
        alpha-oto fetch-coinbase --product "${ASSET}-USD" --days 365 \
          --granularity 3600 --out "$CSV"
    fi
    alpha-oto audit-data --csv "$CSV" --interval-seconds 3600 \
      --out "artifacts/${ASSET}_audit.json"
    # Gaps remain explicit. Lab discards any feature/label window crossing them.
    alpha-oto ml-research --csv "$CSV" \
      --out "artifacts/ml/${ASSET}" \
      --models "${ALPHA_MODELS:-logistic,histgb,forest}" \
      --horizons "${ALPHA_HORIZONS:-1,4,12}" --interval-seconds 3600 \
      --side-cost-bps "${ALPHA_SIDE_COST_BPS:-25}"
done
python scripts/summarize_research.py artifacts/ml/BTC/research_report.json artifacts/ml/ETH/research_report.json
