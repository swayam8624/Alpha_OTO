#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e . -r requirements-ml.txt
python -c 'import sklearn, numpy, joblib; print("Scientific ML ready:",sklearn.__version__,numpy.__version__)'
python -m unittest discover -s tests -v
printf '\nLocal ML dependencies installed. No broker or cloud services involved.\n'
