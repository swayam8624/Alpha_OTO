# Operating costs, checkpoints and reinvestment

## Current checkpoint: ₹0 required

The code uses Python's standard library and optional public data. Continuous
trading servers, broker APIs, paid feeds, premium LLMs and special hardware are
NOT purchased or provisioned. Running locally still incurs electricity and
existing hardware cost. Public APIs may rate-limit or change usage rights.

## Illustrative future monthly cost bands (not contractual quotes)

| Stage | Indicative INR/month | Why/trigger |
|---|---:|---|
| Offline research | ₹0–1,000 | Existing hardware; optional backups |
| One broker research + minimal VPS | ₹1,500–4,000 | Requires approved provider/IP, real data |
| Global execution/monitoring | ₹6,000–20,000 | Multiple market subscriptions, reconciliation |
| Advanced tick/depth + inference | ₹25,000+ | Vendor license and usage dependent |

Brokerage, trading taxes, market impact, foreign remittance/FX costs, and
trading capital are excluded. No amount of cloud spend guarantees profit.

## Cloud AI policy

Use local statistical methods for signals and local LLMs for selected research
summaries. Explicit cloud approval is required to enable paid LLM services.
There is no API client, key requirement or background cloud billing in v0.1.

## Hardware reinvestment policy

Let P = realized after-fee operating/trading profit for the accounting period;
T = tax allowance still to be set aside; L = uncommitted liquid cash;
F = emergency cash floor; r = reinvestment share chosen by owner.

```
capital_expenditure_budget = min(max(0,P-T) * r, max(0,L-F))
```

Default `r = 0.20`. A positive unrealized P&L does not qualify. A negative
realized month generates ZERO budget. No purchases or transfers are automated.
Human review is required to authorize spend, assess after-tax cash, and verify
that upgrading hardware is more valuable than retaining cash or investing it.

## Upgrade sequence (only if workload justifies it)

1. Existing M2 Pro + encrypted external SSD once licensed datasets need space.
2. Reliable always-on CPU gateway with UPS, firewall and static IP once running
   simulations/paper services continually is genuinely necessary.
3. Local home server with ECC RAM, redundant storage, backups and access
   controls once modeling/data throughput exceeds existing capabilities.
4. GPU workstation only after measurable model-latency/quality bottlenecks.
5. Paid cloud LLM or rented GPUs only for experiments where local computation
   is demonstrably inadequate and budget-approved.

**Do not finance hardware based on a speculative future return projection.**
