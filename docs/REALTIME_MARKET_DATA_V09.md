# Alpha_OTO v0.9 — Public Level 2 Market Data and Operational Watchdog

**Status: read-only market data and simulation only. No live orders, broker authentication, customer funds, or verified economic edge.**

## Scope and trust boundary

This release adds the public Coinbase Exchange Level 2 WebSocket adapter, deterministic order-book reconstruction from snapshots and absolute-size `l2update` messages, a raw tamper-evident event journal, best-bid/ask publication into the *simulated* independent risk service, and a read-only operational watchdog. It does not demonstrate that Coinbase trading is available to an India-based account or that public-data rights cover every intended commercial use.

References: [Coinbase Exchange WebSocket overview](https://docs.cdp.coinbase.com/exchange/websocket-feed/overview) and [channels](https://docs.cdp.coinbase.com/exchange/websocket-feed/channels). The feed is transported over TLS, but messages are not signed exchange attestations. Local sequence numbers are NOT guaranteed gap-free exchange sequence numbers.

The snapshot initializes the book, but because snapshot frames may lack an exchange timestamp, the code does not publish a quote until receiving a timestamped update. Stale, inconsistent, future-dated, out-of-order, malformed, or crossed quotes are rejected. Heartbeats do not renew the quote freshness.

After disconnect, a failed append to the raw journal, exchange error, unexpected product, or inconsistent book, the bridge permanently quarantines that feed generation. The last cached bid/ask cannot authorize new orders, even if its timestamp is recent. Restoring data requires a new audited feed generation and complete snapshot; the code never silently clears a quarantine.

## Installation and tests (Apple Silicon Mac)

```bash
cd "$HOME/Desktop/All Might/Alpha_OTO/Alpha_OTO"
source .venv/bin/activate
git pull --ff-only
python -m unittest discover -s tests -v
PYTHONPATH=src python scripts/l2_isolated_smoke.py \
  --outdir "artifacts/production/l2_$(date +%Y%m%d_%H%M%S)"
```

The smoke test uses **fabricated** BTC-USD prices. It runs a separate risk process, preauthorizes one simulated order, triggers an intentionally lost broker acknowledgment, imports two partial fills, reconciles both simulated SQLite authorities, disconnects the feed and confirms that a fresh attempted order is blocked. The watchdog must change GREEN to CRITICAL. This does not assess live fill quality.

## Offline replay

The bundled fixture contains `{"received_at":"...","message":{...}}` JSON objects, one per line. Each run needs fresh database/journal names; the feed is quarantined at completion.

```bash
RUN="$(date +%Y%m%d_%H%M%S)"
python -m alpha_oto.production.marketd replay \
  --trace tests/fixtures/coinbase_l2_trace.jsonl \
  --feed "artifacts/production/replay_$RUN.sqlite3" \
  --raw-journal "artifacts/production/replay_$RUN.jsonl" \
  --product BTC-USD

python -m alpha_oto.production.marketd verify-journal \
  --raw-journal "artifacts/production/replay_$RUN.jsonl"
```

The journal hashes the original local receipt timestamp and raw market envelope, and uses append-only writes. Hash chains detect accidental edits but are not cryptographic signatures or trustworthy remote archival.

## Bounded live *read-only* capture

Requires the optional free library `python -m pip install 'websockets>=14,<17'`. This is a public market-data subscription, **not** a Coinbase account integration.

```bash
mkdir -p private_data/market
RUN="$(date +%Y%m%d_%H%M%S)"
python -m alpha_oto.production.marketd capture \
  --product BTC-USD \
  --seconds 60 \
  --limit 10000 \
  --feed "private_data/market/feed_$RUN.sqlite3" \
  --raw-journal "private_data/market/events_$RUN.jsonl"
```

Bounded capture **has not been exercised against the live endpoint in the offline build environment**. Network restrictions, provider terms and throughput should be verified locally. At the end of the capture or on failure, the feed is sealed/quarantined. Full L2 updates and fsynced journal writes can be expensive under high message volume; sustained production collection requires measured capacity, back-pressure, rotation, monitored disk quotas and operational alarms.

## Independent operational watchdog

```bash
python -m alpha_oto.production.market_watch \
  --socket /path/to/running/risk.sock \
  --disk-path . --min-free-mb 256 \
  --audit-file artifacts/production/market_watch.jsonl
```

Exit code 0 means *simulator checks green*. Exit code 2 means CRITICAL: risk daemon unavailable, risk service not green, quarantined or missing quote source, unexpected live-trading permission, or insufficient disk reserve. The watchdog does not restore services, send notifications, clear quarantines or bypass the risk authority. Supply your own supervised scheduling and alerts before unattended operation.

## Missing production gates

There is still **no real broker adapter, authenticated trading account, approved execution router, licensed executable quote and historical depth validation, market-specific fee/tax accounting, robust service supervision, alerting, independent security review or demonstrated after-cost profitability**. Never infer real execution authorization from working market-data plumbing. Maintain the forward-only evidence process and separate simulation risk budgets from personal capital.

## v0.9.1: Public Advanced Trade L2 capture correction

The old Coinbase Exchange `level2` channel requires authentication. Public,
read-only capture now defaults to `wss://advanced-trade-ws.coinbase.com`, which
accepts subscriptions without any user or trading keys. The Advanced Trade
protocol has one `channel` per subscribe request and emits `l2_data` frames
with a connection-wide `sequence_num`. Each snapshot/update contains
`side=bid/offer`, `price_level`, `new_quantity` and `event_time` records.
It cannot be decoded as Exchange's older `snapshot`/`l2update` wire protocol.

The adapter journals the **original** frames and verifies sequence continuity
across all subscribed channels, including heartbeats, before publishing quotes.
It requires a full snapshot followed by a timestamped update. A gap, duplicate,
provider error, stale or future-dated update, crossed book or failed journal
write quarantines the run. Start another run with **fresh output paths**;
no silent fallback or trading access is enabled.

The CLI defaults to the public Advanced Trade source for `capture`; legacy
`replay` stays on the Exchange format unless `--source advanced` is given.
On provider refusal, the command now emits the provider's bounded
`message`/`reason` text with a nonzero exit code, not an uninformative traceback.
The live endpoint still needs network testing on the operator's Mac.

References: https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/websocket/websocket-overview
and https://docs.cloud.coinbase.com/exchange/docs/changelog#2023-aug-01 .
This is market-data collection only, never a broker trading permission.
