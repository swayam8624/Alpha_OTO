# Alpha_OTO v0.7 — Persistent broker-fault recovery (offline only)

**Scope:** resilient local finance-engine development. Two separate SQLite databases deliberately model independent internal and remote simulated authorities. **No API credentials, external order submission, custody, brokerage connection, real market data feed, or live trading capability exists in this change.** Tests establishing local invariants are not evidence of profits.

## What is different from v0.6

Previously, the local order ledger persisted but the mock broker's remote orders, cash and fills disappeared when its Python process ended. That made it impossible to test an accepted order followed by a network timeout *across actual process restarts*. The new `DurableSimulatedBroker` is an explicitly offline broker simulator backed by its own SQLite database, with WAL and synchronous durable commits. Each mutation acquires `BEGIN IMMEDIATE`, loads and verifies the last committed broker state, applies a controlled simulated change, verifies full cash/positions/fill conservation, and commits a new state and hash-chained event **atomically**.

An `accept_then_timeout` injection causes the simulated broker to commit the accepted order and then throw away the acknowledgment. The local gateway responds by marking the order `UNKNOWN` and **never blindly resubmitting**. A subsequent invocation on a different Python process can query the remote SQLite authority, reconcile the order, import previously unaccounted fills, and continue without duplicate order IDs or double-booking.

Broker simulated cash and holdings are independently recomputed from an ordered replay of all fills whenever broker state is loaded. A corrupted state hash, broken event chain, forged/duplicate fill, mismatched order, wrong account currency or inconsistent cash/position history causes a fail-closed exception. Hash chains are **tamper-evident, not cryptographically authenticated**: a privileged actor who can rewrite entire databases could also rewrite hashes. No independent external audit storage exists yet.

## Run the crash and recovery demonstration

Use new file paths. Existing `demo` uses the original memory-only broker; the durable commands use **separate** `--db` and `--broker-db` paths. These database files should NEVER coincide.

```bash
cd "$HOME/Desktop/All Might/Alpha_OTO/Alpha_OTO"
source .venv/bin/activate
git pull --ff-only
python -m unittest discover -s tests -v

# Process A: record a limit-order intent. Remote emulator ACCEPTS it but
# intentionally loses the acknowledgment. Local status becomes UNKNOWN.
python -m alpha_oto.production durable-start \
  --db artifacts/production/durable_client_v07.sqlite3 \
  --broker-db artifacts/production/durable_remote_v07.sqlite3

# Process B: start fresh, find remote order by client id; import 0.2 unit fill.
python -m alpha_oto.production durable-recover \
  --db artifacts/production/durable_client_v07.sqlite3 \
  --broker-db artifacts/production/durable_remote_v07.sqlite3

# Process C: restart again, import final 0.3 unit fill (remaining quantity).
python -m alpha_oto.production durable-complete \
  --db artifacts/production/durable_client_v07.sqlite3 \
  --broker-db artifacts/production/durable_remote_v07.sqlite3

# Process D: compare cash, positions, fills, and order lifecycle WITHOUT
# mutating either authority. Inspect `audit.status` and `audit.issues`.
python -m alpha_oto.production durable-status \
  --db artifacts/production/durable_client_v07.sqlite3 \
  --broker-db artifacts/production/durable_remote_v07.sqlite3
```

The smoke scenario uses **fabricated** BTC-USD bid/ask values (99.99/100.00) with a simulated $10,000 balance. Expected final simulated cash is $9,949.95, simulated BTC inventory is 0.5, two fills of 0.2 and 0.3, total simulated fees are $0.05, and broker `submit` is recorded **once** across all invocations. These are accounting fixtures, not actual market prices or profit.

Commands intentionally refuse to start a new experiment over an existing saved order. You may run `durable-recover`, `durable-complete` and `durable-status` repeatedly; they must not replay fills or resubmit the order. To explore a completely new synthetic scenario, use new file paths. Do not delete a real financial system's journals merely to clear an error.

## Failure-injection and release criteria

Test categories include accepted-before-timeout after process restart, missing lookup, repeated reconciliation, partial-fill replay, duplicate fill IDs, conflict with reused client IDs, rejected/cancelled orders after restart, remote state hash alteration, modified event-chain links, mismatched starting funds, unknown external order detection, and a read-only two-authority consistency inspection. Integration tests include separate child Python processes and persisted SQLite files.

`inspect_durable_pair` is a read-only snapshot comparison. It flags divergent account cash, positions, remote/local order states or payloads, mismatched fill IDs/payloads, missing or extra remote orders, and any active risk halt. It does not silently modify accounts to make a report green. The coordinator's explicit `reconcile` operation remains the only workflow for importing authoritative fills into the ledger.

### Known limitations

1. **Not a broker sandbox:** the remote authority is another SQLite file on the same computer. It does not simulate independent network partitions, real exchange matching/queues, venue cash holds, slippage, fees, taxes, settlement, or rate limits.
2. **Still single-currency and simulated:** this is **not** a foreign-exchange ledger, multibroker router, or crypto tax engine. Do not repurpose it to represent NSE/IBKR/CoinDCX live eligibility.
3. **Hash chains are local:** no external notarization, signed checkpoints, or independently secured backup archive. Losing both files or corrupting the filesystem is not automatically recoverable.
4. **No independent risk service:** the existing local risk governor is in-process with the simulated gateway. Isolation, privileged credentials, separate service accounts, kill-switch enforcement, and production deployment remain future blockers.
5. **No proven trading returns:** zero live fills and zero verified economic edge. Software reliability does not change this.
6. **No market-time or instrument lifecycle:** the dummy instrument rules do not stand in for a broker's tick sizes, session calendar, corporate actions, derivative expirations or margins.
7. **Performance limitations:** verification walks the append-only remote event hashes and recomputes its account from fills. This is appropriate for fault-injection testing but not optimized for high-throughput production matching.

## Next priorities

Implement independent read-only and privileged risk boundaries; signed deployment policy, production instrument/market-calendar configuration, realistic bid/ask streaming replay, broker sandbox adapter (subject to legal account and broker access), real fee/tax ledger and independent reconciliation watchdog. None of those should turn live-order permissions on until explicit approval.
