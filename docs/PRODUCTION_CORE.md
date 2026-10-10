# Alpha_OTO production core v0.6 — durable simulated broker (NO LIVE ORDERS)

**Critical scope:** This release adds real software components for order state, financial accounting, and operational failure behavior. It does **not** connect to a real broker, does not use money, and does not establish a profitable strategy. It is not yet a live-ready financial product. The API is intentionally limited to an in-memory broker emulator.

## Installed components

| Code | Function | Safety properties and limitations |
|---|---|---|
| `production/contracts.py` | Decimal-based instrument, executable quote, order intent, and risk limits | Strict typing, positive prices, UTC timestamps, positive lot/tick, optional instrument authorization |
| `production/store.py` | SQLite WAL/FULL journal, orders, fills, cash and position book, balanced accounting postings, audit hash chain | Transactional fill accounting; journal tamper evidence; immutable risk configuration after account initialization; not a signed audit log |
| `production/broker.py` | Simulated broker with its **own** cash/orders/positions/fills | Accepted-but-timed-out and partial fill scenarios; not a real exchange or broker sandbox |
| `production/gateway.py` | Durable local intent -> simulator submit -> confirmed fills -> reconciliation | Stable client IDs; no blind retry after ambiguous submission; unmatched broker orders/cash/positions cause account halt |
| `production/health.py` | Local ledger/mark/disk sanity check | Read-only diagnostics; not an independent watchdog or notification service |
| `production/__main__.py` | Self-contained smoke demo, account status, health checks | No credential flags, no live adapter, no withdrawal API |

## The state machine

`CREATED -> SUBMITTING -> ACKNOWLEDGED -> PARTIAL -> FILLED` is the normal progression; `REJECTED`, `CANCELLED`, and `UNKNOWN` are explicit alternatives. Replays of an existing client order ID must agree *exactly* with its original quantity, side, instrument, limit, frozen model ID and intent timestamp. A lost network response after broker acceptance yields `UNKNOWN`, **not** another submission. A second request first looks up the broker identity; if the submission is still indeterminate, account exposure is quarantined and a human must reconcile. Order and fill state changes are durable local SQLite transactions. All emulated remote state is held independently in the broker emulator.

A broker-accepted order may fill at an unfavorable moment or receive partial fills. The store imports each broker-confirmed fill ID at most once, then adjusts held units, average-cost basis, cash, realized **gross** P&L and separately disclosed fees within a single transaction. A sell cannot create an inventory deficit. Ledger posting groups balance to zero in the same quote currency; audited cash, cumulative position cost, realized P&L and fees must reconcile to these postings. The emulator has its own independent cash and positions, and reconciliation must match those records. Changes to already-stored fill IDs fail rather than rewriting history. Journal hash chaining detects accidental changes to event payloads but does **not** provide strong externally anchored tamper resistance.

## Risk protections implemented

- The simulator accepts only explicitly whitelisted same-quote-currency instruments; no leveraged, short, option, contract expiration, multi-venue or FX settlement functionality is implied.
- Strict timestamp and market quote freshness checks; bid/ask spread, quoted size and limit crossing; maximum intent age; instrument lot and tick compliance.
- Pretrade maximum order, symbol and aggregate position notional; reserved cash for unfilled BUY intents; inventory reservations for pending SELL intents; minimum unreserved cash; pending order count and daily order count caps.
- UTC-day **realized net** loss cap (realized gross minus simulator fees); independent portfolio peak-value drawdown circuit breaker based on the last trusted bid marks. These are illustrative safety controls, **not guarantees** of a loss ceiling. Overnight/future fills, market impact and gaps can exceed simulated limits.
- A broker submission in `UNKNOWN` or `SUBMITTING` prevents accepting additional exposure until resolved. Unrecognized broker orders, missing broker fills, account currency/cash/inventory divergence and journal corruption cause failures; critical reconciliation divergence latches an emergency halt. The halt has no arbitrary model-accessible reset.
- Ordinary SELL intents can reduce exposure despite a daily realized-loss threshold. The independent emergency halt blocks all new intents; an actual deployment would need separately authorized risk-reducing liquidation/cancel privileges and an operations escalation path before live approval.

## Run without a subscription or broker account

In your existing checkout:

```bash
cd "$HOME/Desktop/All Might/Alpha_OTO/Alpha_OTO"
source .venv/bin/activate
git pull --ff-only
python -m unittest discover -s tests -v

# Create an offline simulation database and exercise partial fills.
python -m alpha_oto.production demo --db artifacts/production/simulation.sqlite3

# Inspect authoritative simulated cash/positions/orders + hash/accounting integrity.
python -m alpha_oto.production status --db artifacts/production/simulation.sqlite3

# Health may report STALE_POSITION_MARK once the demonstration quote is old.
# This is correct: live eligibility is always false.
python -m alpha_oto.production health --db artifacts/production/simulation.sqlite3
```

The demo refuses to replay its simulated broker against an existing populated journal because the emulator is in-memory and cannot reconstruct authoritative external order state across a computer restart. To run a new demonstration, use a **new DB filename**; do not delete active experiment journals to reset a test. `status` always audits persisted data but does **not** imply an external broker has recovered. The CLI starts only a simulated account and exposes **no trade command for live capital**.

## Operator failure runbook

1. **Unknown submission or lost broker response:** stop new exposure; query the broker's authoritative order ID and original client ID; never blindly retry. If the broker cannot prove the order did not execute, keep the account halted.
2. **Broker cash/position discrepancy:** halt immediately; obtain authoritative broker tradebook, positions, order events, fees, deposits/withdrawals and session; reconcile each fill ID. Do not delete or edit local events to match the desired result.
3. **Stale book, missing quotes, distorted spread, venue closure:** block new orders. Trust no last-seen candle price for executable sizing; reconcile with fresh venue quotes and current trading permissions before resumption.
4. **Disk or SQLite corruption:** halt; preserve database and `-wal`/`-shm` files, copy a forensic image, compare last known authenticated backup and broker authoritative state; restore only via documented review and order reconciliation.
5. **Unexpected drawdown or risk stop:** immediately block new risk, notify the operator, and handle any liquidation through a separately approved reduce-only path. Stop-market order triggers do not guarantee exit prices.
6. **Model changes:** freeze model versions and risk caps, run isolated evaluation; promote only by explicit operator action. Do not allow a model/LLM to alter its own policy or broker authorization.

## Missing before real deployment

This is **not** an independent privileged risk service. It is a separately implemented gateway rule set inside one Python application. The simulated broker is not durable, is not connected to any venue, and has no production exchange calendar, lot-precision per broker, real-time quote stream, actual fees/taxes, FX/custody model, settlement rules, stop order model, tax lots, model-signing infrastructure, security audit, isolated host, redundant watchdog, backup restoration tests, disaster recovery, supported broker SDK, sandbox certification, or compliance approvals. The monitor is a local self-check with no alert delivery or uptime SLA. The SQLite `risk_limits` hash detects differing supplied settings on reopen but is not a cryptographic operator approval signature. UTC midnight is used for the simulated daily loss window; actual venue/session accounting requires an approved local exchange-calendar policy.

The system **must not** place real-money orders until the above blockers, actual broker fills/recovery, economic performance, and applicable regulatory checks are finished. The project remains a life-critical engineering effort, but profitability claims require independent forward evidence, not merely good unit tests.
