#!/usr/bin/env bash
# Research-only orchestrator. Never inserts broker credentials or places trades.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PWD}/src${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p artifacts/omega
files=(private_data/BTC-USD_1h.csv private_data/ETH-USD_1h.csv)
for i in "${!files[@]}"; do
  file="${files[$i]}"
  if [[ ! -f "$file" ]]; then
    if [[ "$i" == 0 ]]; then product=BTC-USD; else product=ETH-USD; fi
    echo "No local $file found: fetching public research-only hourly candles."
    python -m alpha_oto fetch-coinbase --product "$product" --days 365 \
      --granularity 3600 --out "$file"
  fi
done
python -m unittest discover -s tests -v
python -m alpha_oto quant-tournament \
  --csv "${files[0]}" --csv "${files[1]}" \
  --out artifacts/omega/quant_tournament.json
python -m alpha_oto quant-stress \
  --csv "${files[0]}" --csv "${files[1]}" \
  --out artifacts/omega/stress.json
# Cross-asset training can take more CPU; use smaller grid unless requested.
python -m alpha_oto cross-ml \
  --csv "${files[0]}" --csv "${files[1]}" \
  --models "${OMEGA_MODELS:-ridge,histgb}" \
  --horizons "${OMEGA_HORIZONS:-4,12}" \
  --out artifacts/omega/cross_ml
# Online expert weighting is shadow-only and cannot issue orders.
python -m alpha_oto shadow-swarm \
  --csv "${files[0]}" --csv "${files[1]}" \
  --out artifacts/omega/shadow_swarm.json
# Spread hypotheses contain hypothetical short legs and are NEVER live-eligible.
python -m alpha_oto pairs-research \
  --csv "${files[0]}" --csv "${files[1]}" \
  --longest-contiguous-segment \
  --out artifacts/omega/pairs_research.json
python scripts/summarize_omega.py artifacts/omega
