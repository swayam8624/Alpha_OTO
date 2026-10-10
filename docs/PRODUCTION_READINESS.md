# Alpha_OTO — Production Readiness, Release Gates, and Critical Path

Status: **NOT LIVE-READY**. Scope: proprietary automated trading software for an Indian resident, initially one approved venue/instrument class. This is a production engineering specification, not an earnings forecast or a trade recommendation. Last audited: 2026-10-10.

## Current verified inventory

- Repository includes local OHLCV import/audit and append-only Coinbase public historical candles, local logistic / scikit-learn / LightGBM / XGBoost training, backtests, cross-asset regressors, adaptive shadow agents, simplified same-quote-currency spot portfolio accounting, hypothetical pairs work, synthetic options replay, and a forward-only paper-intent journal.
- The latest user-supplied run completed 102/102 unit tests, appended six completed hourly bars per symbol, created an immutable forward experiment on October 10, and reported zero *new* completed forward bars immediately afterward. This is expected for an experiment just frozen; it is not validation evidence.
- Existing BTC/ETH historical data have ten unrecovered hourly gaps per asset. The research code avoids fabricating prices. The original historical holdout has been examined and is not a reusable clean test set for later strategy selection.
- Prior representative holdout: selected breakout strategy -1.9495% net in the modeled simulation vs +18.2171% equal-weight comparator in the same window. Cross-asset model and online swarm did not qualify. These numbers are not predictive.
- There is **no broker execution**, authentication, actual order state machine, broker reconciliation, production watchdog, licensed tick/quote stream, production tax engine, approved instrument permission policy, or trading authorization. Paper fills are candle-price simulations and not broker-confirmed executions.

## Definition of done: production vs profitable business

**Production-capable:** reproducible software, authorized broker access, risk governance, secure deployment, exchange-aware processing, reconciliation, outage handling, monitoring, recovery, tax records, audit logs, runbooks, and verified paper/live integration. No unapproved autonomous alteration of trade authorization or risk budget.

**Economically viable:** separately and independently demonstrated positive after-cost, after-tax, risk-adjusted expected returns, with robust forward observations, accurate execution assumptions, appropriate benchmark comparisons, limited drawdowns and sufficient account capital to cover operating costs. A production-capable app can fail the economic test. The two gates must not be conflated.

**Ultimate goal:** limited-size, compliant, fully observable unattended operation that can refuse unprofitable opportunities. Not guaranteed profits each day or each second. Extreme-tail opportunities must be segregated from ordinary risk capital.

## P0 release blockers: implement in dependency order

### P0.1 Select one legal live venue and instrument class
Owner: product/compliance. Choose an Indian equity broker and cash/ETF account permissions first, *or* an explicitly permitted crypto venue with documented API, custody and Indian tax treatment. Do not presume Coinbase public data establishes Coinbase trading eligibility in India. Get account owner, fee schedule, exchange order-rate requirements, static IP rules, approved order types, hours and lot sizes documented. Keep international cash stocks, crypto, derivatives and forex as separately gated expansions.

**Acceptance:** dated venue-eligibility checklist signed off, with broker confirmation where required; account scope, instrument whitelist and order-rate limits saved as versioned config; disabled by default.

### P0.2 Instrument registry and market clock
Define exchange/venue/symbol/instrument ID, contract version, tick/lot constraints, quote/base currency, local venue trading calendar, session cutoffs, margin/shortability, expiry, split/dividend events and contract roll. Asset symbols must not be assumed to share a global USD cash balance. For foreign holdings implement currency cash subledgers and settled-vs-unsettled balance restrictions.

**Acceptance:** calendar/holiday/DST fixtures; reject out-of-session orders, wrong symbols, lot/tick violations, insufficient settled cash and expired contracts.

### P0.3 Licensed production feed and durable event ingestion
Separate raw append-only market events from validated normalized features. Acquire rights for persistent data storage and trading use. Record source, receive and exchange timestamps, sequence IDs, freshness, feed pauses, gaps, revisions, and bad ticks. Build streaming bid/ask/top-of-book, with depth where required for sizing and optional trades prints. Add reconnect/resubscribe/replay/duplicate suppression. Coinbase hourly OHLCV cannot establish executable opening quotes.

**Acceptance:** injection tests for dropped, duplicated, reordered and stale messages; deterministic resynchronization; fail closed when price/depth/currency is not verifiable; encrypted retained logs with access control.

### P0.4 Event-driven simulator validated against broker paper order flow
Build an order-event simulator using bid/ask and available quoted size, fee schedules, trading hours, latency and partial fills; mark inventory and account exposure at observable executable quotes. Add instrument-specific taxes/charges and funding; ensure no future volume, quote or label can influence historical actions. Simulate disconnections, price gaps, rejections and stop nonfills. Cross-check estimates against a broker-supported paper/sandbox session.

**Acceptance:** golden event replays agree across runs; explicit best/worst execution sensitivity; broker paper fills/rejects reconcile to internal ledger; no OHLC-only strategy is treated as proven tradable.

### P0.5 Financial accounting and tax ledger
Separate realized/unrealized gross P&L, trading fees, exchange charges, spreads, impact, FX, funding, withholding/TDS, income tax provision and final after-tax net P&L. Implement cash movements, transfers, deposits/withdrawals, tax lots and statements. Model Indian crypto VDA restrictions separately from domestic equity, F&O, and overseas investment treatment. Require accountant review before treating the calculation as tax compliant.

**Acceptance:** ledger balances to cash + position marks after every event; exact broker tradebook reconciliation on test fixtures; transaction-level report reproduces each fee and tax category.

### P0.6 Independent risk governor
Position sizing, no hidden shared leverage, maximum drawdown and daily-loss circuit breakers, order-volume participation, exposure/correlation caps, per-market and per-agent caps, instrument whitelist, kill switch, credential scope, maximum pending orders, and market-data freshness thresholds. Prohibit model-generated increases in global risk caps or live permissions. Do not confuse stop trigger with guaranteed fill.

**Acceptance:** property/fault tests prove no orders accepted beyond programmed boundaries; simulated corrupted model, stale quote, missing FX, duplicate order, clock drift, halted venue, and broker disagreement block new risk. Emergency halt is tested.

### P0.7 Broker adapter and durable order lifecycle
Implement an outbound broker adapter **only after** venue approval; separate secrets and privileged process. Durable order-intent IDs, correlation IDs, submit/acknowledge/partial-fill/filled/rejected/cancelled/unknown transitions, idempotent query-before-retry, rate limits, order modifications, order expiry and broker-native protective order limitations. Broker REST/WebSocket recovery may not preserve the same observable ordering; reconcile each transition to broker truth. No withdrawal API permission.

**Acceptance:** integration/sandbox tests across all order transitions; a simulated timeout following broker acceptance cannot create a second order; reconnect produces the same authoritative positions as broker books.

### P0.8 Production runtime and security
Separate local research from small always-on execution and independent watchdog. Authenticated tunnel, static IP where mandated, secret management, rotation and least privilege; encrypted at-rest state and backups; robust database transactions, atomic checkpoints and recovery after power loss. Resource budgets and disk exhaustion alarms. Do not put credentials, personal account identifiers or private research data in public GitHub.

**Acceptance:** documented setup and rollback, restoration from tested backup, OS/server restart and network-partition experiments, successful alerts for a stopped worker, corrupted state or stale prices.

### P0.9 CI, QA, observability and operating procedures
Keep tests but add integration tests with broker mock server, contract tests, historical replay, load/soak tests, OS matrix (Apple Silicon macOS + production Linux), vulnerability and secrets scanning, structured logs, metrics, tracing and command audit. Track broker vs internal inventory divergence, rejected/unknown orders, missing data, deployed-model IDs and risk-halt counts. Define alerts and human escalation instructions.

**Acceptance:** observable uptime/availability service objectives, no unacknowledged critical alerts in a soak test, restore drill completed, every order explainable and matched to a timestamped approved signal/risk decision.

### P0.10 Independent economic go/no-go evaluation
Freeze candidate strategy definitions, benchmark, fees, evaluation horizon and deployment risk budget *before* unseen evaluation. Compare active agent selection against buy/hold, equal-weight, cash, volatility targeting and an appropriate fee-adjusted index. Include multiple regimes and practical executable quotes. Correct for trying many agents; count trades and independent bets, not just candles. Evaluate calibration and trading coverage.

**Acceptance:** predeclared minimum economic viability criteria signed off after truly new observations; sufficient independent trades and regimes; favorable results under plausible worse-cost/impact scenarios; no unapproved release if the gate fails. 720 synchronized hourly candles alone does not demonstrate an investable edge.

## Staged deployment (all transitions explicit, never autonomous)

- **S0 offline:** current state; unit/integration fault tests and public history.
- **S1 forward shadow:** predictions timestamped *before* outcomes and fully observed; compare against untouched baselines. No orders.
- **S2 broker sandbox/paper:** broker-native market feeds and simulated orders; reconcile statuses and execution frictions for multiple adverse conditions. No money.
- **S3 operator-confirmed micro live pilot:** separately approved account, compliant venue, no leverage by default, capped exploratory loss budget that can be lost in full, manual kill switch. Log true net P&L and compare to S2. Do not use living-expense/borrowed/emergency capital.
- **S4 limited automated live:** only after demonstrated economic value and operational reliability at S3, with hard global controls. Any unexpected divergence, severe drawdown, failed reconciliation or stale prices automatically returns to safer mode.
- **S5 scaled portfolio:** progressive limits approved by a human and supported by actual fill quality, stable performance, tax/capital efficiency and venue capacity. Never scale only because one week was lucky.

## P1 product expansion

Add NSE session stocks and optionally domestic commodities with corporate actions, sector exposure, exchange holidays and instrument expiries. Add overseas cash securities through an eligible account with funding/FX/settlement and foreign reporting. Crypto market selection requires lawful venue access, effective taxable after-cost edge, approved asset custody and withdrawal safeguards. Cross-venue funding cannot be assumed instantaneous. More sophisticated ML, calibrated probabilities, regime filters and adaptive allocation must pass the same forward-selection gate as simpler strategies.

## P2 extraordinary-upside module

TailHunter should be a completely separate budget with premium-defined maximum intended loss, actual contract terms, strikes/expiry/lot size, executable historical option quotes/depth, spread and implied-volatility shocks, gap risk, tax and liquidation rules. A past chart showing 100x does not prove an executable buy and sale or a strategy that predicts it. No naked short-option exposure in this module.

## P3 hardware/cloud investment

Existing Apple Silicon + local Ollama/gradient-boosted models suffice for initial R&D; use a small approved always-on gateway only when required by operational tests and static-IP rules. Additional cloud LLM usage only if a frozen experiment demonstrates value net of API charges. Use verified **realized after-tax profit**, adequate liquidity reserves and explicit approval for SSD/GPU/home-server upgrades; never spend paper profits or assume next month will finance the server.

## Release dashboard: report these separately

1. **Engineering:** tests by class, CI OS coverage, successful recoveries, feed lag, uptime, crash count, order-state unknowns and broker reconciliation divergence.
2. **Decision:** sample count, turnover, calibration, signal latency, data completeness, purged walk-forward results and multiple-testing adjustment.
3. **Economics:** gross realized trading P&L, explicit brokerage, spread/slippage, modeled-to-actual execution gap, taxes, infrastructure/AI spend, *net* cash profit, max drawdown and probability-of-ruin stress.
4. **Capacity:** deployed risk notional, market depth consumed, estimated slippage vs size, settlement capacity, exchange rate-limit headroom.
5. **Governance:** approvals, model versions, strategy-change log, risk-limit-change log, credentials/access review and regulatory review dates.

## Why the October 10 forward snapshot is not a failure

The latest supplied state had just created a freeze, submitted an intent for the next future hourly open, and reported zero newly completed forward bars because the corresponding observation had not yet completed. The zero P&L, fills and drawdown are exactly what a newly frozen, not-yet-observed strategy should report. The forward script must be reliably scheduled and observed for *future* bars; it currently is not a 24/7 unattended service.

## Non-negotiable release principles

- A green unit-test suite is not a security audit, production load test, broker certification or proof of returns.
- Failing profitability criteria means no autonomous deployment, even if software is complete.
- Local language models are research tools, not order authorities or trusted accountants.
- Execution needs broker-confirmed prices/orders; Coinbase public candles are research data.
- Capital and people's livelihood are not acceptance-test resources.
