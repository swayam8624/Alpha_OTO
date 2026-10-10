# Alpha_OTO v1.0 Local Operations Console

## What the product actually supports

A single-command, browser-based local control panel for your existing Alpha_OTO engine. It ships with Python's standard library: no frontend package manager, SaaS host, database server, Google account, or web dashboard subscription. Existing scientific Python packages remain necessary for the ML jobs. On an Apple Silicon Mac with a cloned repository and `.venv` already set up:

```bash
cd "$HOME/Desktop/All Might/Alpha_OTO/Alpha_OTO"
git pull --ff-only
bash scripts/start_alpha_oto.sh
```

The launcher starts a small Python HTTP server on **127.0.0.1:8765** and opens your browser with a random one-time launch URL. The login handler redirects to `/`, keeping the token out of the address bar after entry. The browser uses an HttpOnly SameSite=Strict cookie, anti-CSRF header, and a strict same-origin requirement for all mutations. Server responses include CSP, frame denial, no-referrer and no-cache headers. The web app uses no external scripts, fonts, CSS libraries or telemetry. It does **not** bind to a public host.

You can choose a different local port with `bash scripts/start_alpha_oto.sh --port 8791` or skip browser launch with `--no-open`. Stop the console with Control-C. **Do not reverse-proxy or expose it on the public internet.** This is a same-user localhost operations tool, not a remote multi-tenant security perimeter. The local Unix risk daemon is likewise not a hardened privileged trading boundary.

## Pages

| Page | Real function |
|---|---|
| Overview | Inventory of local public-market data, forward-only evidence, local research report state, and last jobs. No fabricated live balance. |
| Market data | Read-only list of BTC/ETH files, refresh completed public historical candles, initiate a 30-second Advanced Trade public L2 capture, and list capture journals. |
| Strategies | Show existing tournament, cross-asset ML, stress, and swarm report presence; invoke the existing offline Omega suite or existing forward step. |
| Operations | Run allowlisted system-test, data, research, simulated broker and L2 risk-integrity workflows; read job log excerpts; enable or disable hourly forward-only paper observations. |
| Setup & connections | Store local market/broker *preferences* and a simulated-money budget; show which external KYC, broker permission, payment and compliance gates are still unavailable. |

Operations run in the background on the local Python service and save job states in `private_data/dashboard/operator.sqlite3`, with console logs under `artifacts/dashboard_jobs/`. Only one engine job can run at a time. Job state is marked `INTERRUPTED` after a server restart if it was left running. Commands have hard limits and fixed timeouts; user-entered shell commands or user-provided executable arguments are forbidden. If a job fails, read the job log. The GUI never changes the existing forward research state by resetting or replacing it.

**Scheduler caveats:** The hourly forward step runs after about minute 02 UTC of each hour only while the Python console service is running and the original `private_data/forward/forward_experiment.json` exists. The browser tab itself does not need to stay open. Sleeping the Mac, a network outage, or stopping the web server interrupts observation. The forward protocol intentionally refuses to invent missed historical signals/fills. Do not mistake this for always-on infrastructure.

## Enabling the existing paper experiment

If no frozen experiment exists, first acquire public completed BTC/ETH OHLCV and use the existing immutable command:

```bash
alpha-oto data-update-coinbase --csv private_data/BTC-USD_1h.csv
alpha-oto data-update-coinbase --csv private_data/ETH-USD_1h.csv
alpha-oto forward-freeze \
  --csv private_data/BTC-USD_1h.csv \
  --csv private_data/ETH-USD_1h.csv \
  --state private_data/forward/forward_experiment.json
```

**Do not repeat `forward-freeze`** on the same file; it intentionally refuses overwrites. Open the Operations page, select *Start hourly paper monitor* and leave the underlying Python server running. Use the log list to investigate failures. An experimental ledger is not a real brokerage cash balance.

## External authentication, real-money payments and trading

**Not integrated.** The GUI **never** asks for brokerage passwords, authentication tokens, bank details, tax IDs, payment cards, API secrets, or withdrawal permissions. Selecting Dhan or IBKR records a plan only. It does not log in, request or receive OAuth authorization, fund the account, buy market data, or authorize even a minimum live order. It cannot make purchases or fees payable within Alpha_OTO. No fake OAuth or payment buttons are presented. Future integrations must be separately reviewed against the chosen broker's actual supported API, verified client eligibility, the account's permitted instruments and countries, 2FA/KYC requirements, exact user-approved charges and transaction terms. Credentials should be stored only via a dedicated, well-reviewed OS secret vault or an appropriately isolated agent, never in the local web UI's SQLite config or GitHub.

Any future live feature must be independently approved and must not rely on *only* a successful paper backtest or green unit suite. Add real broker sandbox/paper integration, order permissions, account reconciliation, approved broker endpoint and secure credentials, real quote/fill verification, production operations, regulatory checks, independent risk governance, comprehensive fee/tax handling, and forward evidence of genuine after-cost strategy performance. The existing return studies did not establish a profitable strategy.

## Testing

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m unittest discover -s tests -p test_dashboard.py -v
```

The GUI suite verifies cookie-based login, CSRF and origin defenses, rejection of unknown commands or secret fields, setup persistence, job exclusion, interrupted-job recovery and denial of live-trading routes. Passing tests establish software invariants only.
