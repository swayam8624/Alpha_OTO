# Alpha_OTO v1.1 — DhanHQ account inspection and one-click paper monitoring

**Release scope: Indian cash-equity broker onboarding, verified account inspection only. All real orders, deposits, withdrawals and payments remain disabled.** This release adds a genuine DhanHQ v2 account verification integration, not a fictional status badge, and a guarded paper monitoring start action to the existing localhost browser console. It does not make the previously unsuccessful strategies profitable or validate their operation in Indian equities.

## Run the product

```bash
cd "$HOME/Desktop/All Might/Alpha_OTO/Alpha_OTO"
source .venv/bin/activate
git pull --ff-only
bash scripts/start_alpha_oto.sh
```

Open the browser using the **private localhost login URL** that appears in the Terminal. The website stays at `http://127.0.0.1:8765` and has no cloud backend. Never expose it via a tunnel, shared LAN bind, container public port or reverse proxy; it is a single-user local control console, not a hardened production custody solution.

## Dhan account onboarding

1. Visit [Dhan Web](https://web.dhan.co/), open or verify your brokerage account directly with Dhan, and complete all KYC and bank verification there. The app does not collect passwords, PAN, bank account numbers, PINs or TOTP codes.
2. In Dhan Web, navigate to your profile's DhanHQ API access section. Generate an individual API **access token** (normally valid 24 hours). Dhan's Trading APIs are available without a separate API fee for eligible Dhan users; optional Data APIs have a separate subscription and must be purchased exclusively through Dhan. Prices and terms can change.
3. Launch Alpha_OTO and open **Dhan account** in the sidebar. Enter your numeric Dhan client ID and your access token. The local server sends a GET request to `https://api.dhan.co/v2/profile` and compares the returned `dhanClientId` with your input. Authentication fails closed on a mismatch, expired token, invalid format, network error or unexpected response. The app **does not generate** tokens, and does not collect your Dhan PIN or TOTP.
4. After connecting, select **Refresh real account snapshot**. This makes exactly four fixed read-only GET requests to Dhan `/fundlimit`, `/holdings`, `/positions`, and `/orders`. The GUI displays broker-reported available/used cash, summary quantities, holdings and current orders. Broker balances are live-account information, not model profits. This snapshot is requested by a direct operator action; no background polling or broker state mutation happens.
5. Select **Disconnect** or close the Python server to discard the access token. No token, PII, order records, positions, or real broker balances are saved by Alpha_OTO to SQLite, JSONL, job logs, browser localStorage, or GitHub. Profile information (masked client ID, segment names and token validity) can appear briefly in browser memory and HTTP responses while the session is open. The `operator.sqlite3` database retains only existing non-secret setup preferences and local research-job metadata.

**Important scope warning:** The broker's access token can provide trading privileges even though this particular application only invokes GET endpoints. These are **application-level read-only restrictions**, not Dhan-enforced token scopes. Do not share that token, use it on an untrusted/shared machine or mistake it for an isolated broker permission boundary. Browser-to-Python traffic uses loopback HTTP, not network TLS; do not open remote access.

## Official API contracts used

- `GET https://api.dhan.co/v2/profile` — verify identity and token: <https://dhanhq.co/docs/v2/authentication/>
- `GET https://api.dhan.co/v2/fundlimit` — available account funds: <https://dhanhq.co/docs/v2/funds/>
- `GET https://api.dhan.co/v2/holdings`, `/positions` — portfolio: <https://dhanhq.co/docs/v2/portfolio/>
- `GET https://api.dhan.co/v2/orders` — current-day order book read: <https://dhanhq.co/docs/v2/orders/>

No HTTP POST, PUT, DELETE or any `orders/*`, deposit, margin, payment, trade-modification, withdrawal or transfer route is present in the connector. The four fixed GET account calls are rate-limited by the provider and bounded by an 8-second timeout / 1-MB response cap. HTTP errors never echo the raw response body (which might contain sensitive information).

## Payments and funding

Dhan handles deposits, withdrawals, bank verification and optional paid market data **entirely on its own website**. The Alpha_OTO UI supplies an official external website link, but **does not** act as a payment processor and cannot spend trading capital. Do not trust unofficial payment links or assume a real broker cash balance means that automated investing has been approved. API/data fees, market access and eligibility must be independently verified with Dhan.

## Start a safe paper experiment from the GUI

After obtaining public BTC/ETH historical research files, click **Start paper monitoring** from Overview. The bounded, allowlisted job runs `scripts/start_paper_once.sh`:

- Refuses to operate without both local datasets.
- Uses the existing immutable `private_data/forward/forward_experiment.json` if it exists, or creates a new freeze using the existing validated `forward-freeze` command. It never overwrites an existing freeze.
- Executes `scripts/forward_once.sh`, which appends public market candles and reconciles precommitted simulated actions.
- Enables once-hourly paper monitoring while the GUI Python process is alive **only after successful completion**. Sleeping the Mac or exiting the server suspends monitoring; missing hours are not rewritten into artificial paper profit.

This experiment uses BTC/ETH public data. It is **not Dhan Indian equity investment**, a portfolio recommendation or a broker order. Future simulation-to-real routing must independently validate Indian exchange calendars, data subscriptions, security IDs, static IP requirements, complete cost and tax accounting, broker sandbox/paper behavior, independent risk/privilege separation and investable evidence.

## Tests and release gates

```bash
PYTHONPATH=src python -m unittest discover -s tests -p 'test_dhan_readonly.py' -v
PYTHONPATH=src python -m unittest discover -s tests -p 'test_dashboard.py' -v
PYTHONPATH=src python -m unittest discover -s tests -v
```

Tests run with a **fake Dhan response transport** and never contact actual broker servers. They verify GET-only URL whitelisting, token rejection, no sensitive error-body logging, mismatched IDs, expired token invalidation, memory-only storage, session clearing, browser cookie/CSRF/origin, disallowed trading/payment routes and paper-only scope. The **actual** Dhan login has not been tested against a real account because no individual Dhan API access token is available in this environment. The interface is ready for that explicit local test.

## No-go criteria before live operation

A verified profile is **not** regulatory permission to deploy automated trades. A broker-approved execution adapter, explicit production account authorization, credential vault with privilege isolation, independent broker-reconciliation and risk circuit breakers, real bid/ask and order fill measurements, SEBI/broker algorithmic trading compliance, all applicable charges and taxes, production operations and truly forward-validated net profitability are required before live mode can be considered. Repeated historical tuning does not create forward evidence.
