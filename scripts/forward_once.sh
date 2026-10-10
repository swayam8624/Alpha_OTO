#!/usr/bin/env bash
# Read-only public data + PRECOMMITTED paper research. NO broker endpoints.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PWD}/src${PYTHONPATH:+:$PYTHONPATH}"
files=(private_data/BTC-USD_1h.csv private_data/ETH-USD_1h.csv)
for file in "${files[@]}"; do
  python -m alpha_oto data-update-coinbase --csv "$file"
done
state=private_data/forward/forward_experiment.json
if [[ ! -f "$state" ]]; then
  echo 'Forward experiment not frozen. First run alpha-oto forward-freeze with the two --csv files.'
  exit 2
fi
python -m alpha_oto forward-step --csv "${files[0]}" --csv "${files[1]}" --state "$state"
