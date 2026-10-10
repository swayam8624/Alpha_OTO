#!/usr/bin/env bash
# PAPER-ONLY launch: reuse an existing frozen experiment or create one after data verification.
# Does not authorize real brokerage, deposits, derivatives or orders.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PWD}/src${PYTHONPATH:+:$PYTHONPATH}"
files=(private_data/BTC-USD_1h.csv private_data/ETH-USD_1h.csv)
for f in "${files[@]}"; do
    if [[ ! -f "$f" ]]; then
        echo "Cannot start paper monitoring: missing $f. Refresh data in Market data first."
        exit 2
    fi
done
state=private_data/forward/forward_experiment.json
if [[ ! -f "$state" ]]; then
    echo "No existing forward experiment: freezing models before future outcomes."
    python -m alpha_oto forward-freeze \
        --csv "${files[0]}" --csv "${files[1]}" --state "$state"
fi
bash scripts/forward_once.sh
