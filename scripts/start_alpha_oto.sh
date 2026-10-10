#!/usr/bin/env bash
# One command: launch browser GUI. Loopback only; no broker trading or payments.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -x .venv/bin/python ]]; then
  PYTHON=.venv/bin/python
else
  PYTHON=python3
fi
export PYTHONPATH="${PWD}/src${PYTHONPATH:+:$PYTHONPATH}"
echo 'Starting Alpha_OTO. The browser will open on 127.0.0.1.'
exec "$PYTHON" -m alpha_oto.dashboard "$@"
