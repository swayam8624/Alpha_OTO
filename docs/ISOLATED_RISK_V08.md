# Alpha_OTO v0.8 — Independent Simulated Risk Service & Quote Feed

**Critical**: all modules in this release are **SIMULATOR ONLY**. There is no real brokerage API, order router, broker authorization, live trade permission, or evidence of a positive trading edge. Every limit and price below is for engineering acceptance tests, not a trade suggestion. The new risk service adds **process separation for simulated order authorization**. It is not a hardened privilege boundary against malicious code running under the same OS user.

## Core design and trust boundaries

1. **MarketRegistry** (`production/market_registry.py`) pins a SHA-256 digest of all instruments and venue-time policies. Simulated instrument tick, lot, max size, currency and whitelist are checked. Explicitly supplied weekly sessions, holidays, timezone/DST and calendar-validity dates fail closed outside their schedules. No automatically inferred stock-exchange holiday calendars are supplied. The included fixture is SIMULATED, BTC-USD, 24/7 for testing only.
2. **DurableQuoteFeed** (`production/market_feed.py`) stores full bid/ask, sizes, source and receive timestamps, per-symbol source sequence, hash-chained quote events, separately hash-chained quarantine incidents, and latest snapshot in SQLite WAL. Duplicated identical packets are idempotent; sequence gaps, rewinds, conflicts or clocks moving backwards quarantine the corresponding asset. The feed cannot synthesize prices, resynchronize itself or claim that a quote is executable on a real venue. Replay accepts explicit JSONL event records with a `sequence` and `quote` object.
3. **IsolatedRiskAuthority** (`production/risk_service.py`) runs separately behind a Unix-domain socket (owner-only `0600`). It independently reads the quote feed and ledger, verifies their data integrity, validates the pinned instrument calendar and existing **RiskLimits**, checks timestamps/spread/depth/cash/exposure/order/fee limits via the core ledger, and durably records an approved *simulated* intent before any fake broker dispatch. The request cannot supply a price, quote, risk limit or server timestamp. API operations are `HEALTH`, `AUTHORIZE`, and one-way emergency `HALT`; there is no unhalt RPC.
4. **SimulationGateway** and **DurableSimulatedBroker** remain fake market/order systems. `SeparatedSimulationGateway` asks the risk service to create an approved durable order, then uses the existing fake gateway. A separate demo executes a lost acknowledgment and two partial fills while keeping broker and ledger reconciliation consistent. A real order-routing security boundary would require broker credentials exclusive to a broker service, mandatory authorization attestation and account protections; this release has none.

### Important security limitations

- The socket protects against unrelated processes with different Unix-user permissions, **not** malicious processes sharing the account/UID, root compromise, malicious filesystem alteration, or direct database editing. SHA-256 chains offer accidental tamper evidence, not signatures or cryptographic protection against full rewrite of both log and metadata.
- The feed is offline replay / injected test quotes; source identities are not authenticated against a licensed exchange. The operator is responsible for feed acquisition permissions and provenance.
- An agent can still import Python modules directly on the same trusted machine. **Do not attach a live broker adapter to the existing fake gateway.** A future privileged router must enforce independently signed/bound risk decisions before issuing real orders.
- The risk daemon halts new simulated buys and sells on its explicit emergency halt (unlike the separate daily-loss protection that can allow reducing exposure). On a genuine trading system, sell-only emergency flattening must be a separately designed and broker-tested control.
- Crash/power-loss across multiple SQLite WAL files on one host is not equivalent to transactional multi-venue durability. The two-system reconciliation protocol remains necessary.
- Every quote and balance check operates at the time of authorization; market price may move before a future venue acknowledgment. The daemon does **not** guarantee a price or fill and cannot prevent future slippage, gaps or default losses.

## Installation & complete two-process smoke run — ₹0 additional fees

The installed Python 3.11+ environment suffices. No Ollama/cloud AI, vendor API key, market-data subscription or real funding is needed.

```bash
cd "$HOME/Desktop/All Might/Alpha_OTO/Alpha_OTO"
source .venv/bin/activate
git pull --ff-only
python -m unittest discover -s tests -v

# Choose a fresh output path; do NOT overwrite previous financial evidence.
PYTHONPATH=src python scripts/risk_isolated_smoke.py \
  --outdir "artifacts/production/isolation_$(date +%Y%m%d_%H%M%S)"
```

The smoke program generates a synthetic L1 quote, persists it in a separate quote database, starts the risk authority as an actual child process, authorizes a fake order via its Unix socket, simulates broker acceptance with lost acknowledgment, imports two fills, verifies no duplicate submission, and independently audits remote cash/positions. It retains SQLite files and a `run_report.json` in the new output directory. To avoid macOS's small Unix-socket pathname limit, the socket lives in a separate private temporary directory and is removed when the daemon exits.

Expected smoke invariants: one broker submission, two partial fills, simulated USD 9,949.95 remaining cash, 0.5 fake BTC units, reconciled ledger, `live_approved=false` in service diagnostics. Never interpret the synthetic BTC price of $100 as a real price.

## Individual service commands (advanced engineering tests)

Create a fresh isolated test directory and point the service to `config/simulator_market.json`. Only start a daemon against a dedicated newly initialized simulation database:

```bash
# Start a server in one terminal (will block until stopped)
PYTHONPATH=src python -m alpha_oto.production.riskd serve \
  --ledger artifacts/production/risk_test_local.sqlite3 \
  --feed artifacts/production/risk_test_quotes.sqlite3 \
  --registry config/simulator_market.json \
  --socket /tmp/alphaoto-risk-operator.sock

# In a second terminal, check status or request an emergency halt
PYTHONPATH=src python -m alpha_oto.production.riskd health \
  --socket /tmp/alphaoto-risk-operator.sock
PYTHONPATH=src python -m alpha_oto.production.riskd halt \
  --socket /tmp/alphaoto-risk-operator.sock
```

`authorize` needs a previously ingested, recent, timestamped and verified L1 quote plus an `--intent-json` payload with exactly `client_id`, `symbol`, `side`, `quantity`, `limit`, `created_at`, `strategy`, `model_version`. An intent must have been created no more than the strict ledger-limit age ago (10 seconds by default). This is intentional. The service will refuse an expired, stale or unapproved transaction.

For offline quote replay, each newline of an input trace must resemble:

```json
{"sequence":1,"quote":{"symbol":"BTC-USD","bid":"99.99","ask":"100","bid_size":"10","ask_size":"10","source_time":"2026-10-10T00:00:00+00:00","received_time":"2026-10-10T00:00:00+00:00"}}
```

```bash
PYTHONPATH=src python -m alpha_oto.production.riskd ingest \
  --feed artifacts/production/risk_test_quotes.sqlite3 \
  --trace /path/to/your/offline_events.jsonl
PYTHONPATH=src python -m alpha_oto.production.riskd feed-status \
  --feed artifacts/production/risk_test_quotes.sqlite3
```

*Note*: Old demonstration timestamps become stale immediately and **will not** authorize current simulated trades. The smoke script generates fresh local timestamps instead. A feed sequence gap is a quarantine requiring investigation; there is deliberately no automatic unsafe resynchronization API. If the Unix socket remains after an abnormal process exit, verify the old service is genuinely stopped before manually removing the stale socket path. The server intentionally refuses to unlink an existing path.

## Validation requirements before any live integration

- Confirm correct venue order authorization, instrument metadata, holiday/DST calendar coverage, corporate-action/expiry behavior, and current Indian compliance obligations with the chosen authorized broker.
- Separate the broker's live credentials into an OS/service account to which all model/agent processes lack access; use signed/bound, single-use authorization tickets or secure IPC with durable replay prevention.
- Extend the feed to authenticated licensed real-time bid/ask with exchange sequence recovery, burst/load handling, and account-specific routing. Add external watchdog, process heartbeat, warning/action channels and restart drill.
- Run broker-paper integration tests with accepted-but-lost acknowledgments, partial/reordered/duplicate fills, ambiguous remote orders, cancelled-but-filled messages and broker-to-ledger reconciliation.
- Correctly account for venue-specific fees, withholding, FX, funding/borrow, TDS/taxes and settlement. Predeclared forward evidence must demonstrate a positive, robust **net** edge before any real-money release.

## Test coverage in this release

The offline regression suite validates session hours, holidays, session rollover, DST, immutable registry digests, quote sequence and quarantine behavior, event/incident log tampering, timestamp latency, stale market data, fail-closed risk calls, broker-free authorization, policy caps, process-separated RPC, socket owner permissions, explicit halts, and integration with the durable fake order gateway.

The existing v0.7 166-test suite remains an independent baseline; the current v0.8 changes add new tests and execute the full repository CI suite on GitHub. Test success never proves profitability or broker readiness.
