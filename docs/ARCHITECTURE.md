# Architecture and research gates

## Engineering policy

This is a financial research system, not a broker. Source files contain no
trading order APIs, crypto withdrawal endpoints or unsafe automated deployments.
Never optimize an agent for '100x' or 'one profit every second' without a risk
budget: many extreme-payoff events have an overwhelmingly negative expectation.

## Conceptual layers

```
Local permitted OHLCV source -> strict CSV/Parquet warehouse (future)
    -> timestamp-aware features -> independent strategy agents
    -> local model training -> chronological validation tournament
    -> policy/evolution evaluator -> shadow portfolio (future)
    -> independent risk controller -> broker gateway (future, separate project)
    -> reconciliation and immutable audit log (future)
```

Implemented input: CSV + optional read-only public Coinbase candles.
A local read-only watcher (`watch --csv ...`) can run continuously, but does
not refresh those CSVs itself or submit any orders. It does not constitute India/US market-wide coverage.

## Future agent lifecycle

RESEARCH -> VERIFIED_BACKTEST -> SHADOW -> HUMAN_APPROVED -> ACTIVE
                                  |                   |
                              QUARANTINED <------ CRITICAL_FAILURE
                                  |
                               RETIRED

The repo **only** implements research-level candidate selection. No agent is
eligible for active trading based on past backtests alone. An agent that loses
three times in a row is not automatically invalid; evaluate the expected loss
sequence, statistical uncertainty, costs and market regime first.

## Data integrity

- Raw market files immutable and checksummed. Exchange timestamps retained in UTC.
- As-of corporate actions, delistings, symbol changes, holidays and survivor
  bias must be handled before equity claims can be believed.
- Always separate training, validation and final untouched test dates; purging
  must match the maximum prediction horizon.
- Costs must include applicable brokerage, statutory charges, spreads, market
  impact, taxes, exchange rules, and realistic latency. Snapshot data alone is
  inadequate for high-frequency or long-option 100x claims.
- A single backtest winner among many agents is likely to exhibit selection
  bias. Use deflated Sharpe/appropriate multiple-testing diagnostics and fresh
  forward-only periods before promotion.

## Milestones not yet implemented

1. Read-only NSE/BSE and global equity adapters with verified licensed archives.
2. Multi-symbol, multi-asset portfolio cash and settlement ledgers, FX exposure.
3. Purged walk-forward validation, multiple-testing and uncertainty gates.
4. Licensed options/quotes/tick data and executable bid/ask/size replay.
5. Paper brokerage adapters, reconciliation, margin and venue permission models.
6. Production order gateway, independent risk engine, kill switches, logging.
7. Regulatory audit (SEBI, RBI/FEMA, local crypto tax, foreign assets).
8. Independent monitoring, homelab failover, operational disaster recovery.

## Security

GitHub repository public: never put API keys, model proprietary weights,
historical purchased feeds, broker accounts, positions or customer data here.
Use OS keychain or a secret manager only after a specific provider is selected.
Separate trading and withdrawal permissions; disable withdrawals when the
venue supports it. Cloud inference is not in the execution path.
