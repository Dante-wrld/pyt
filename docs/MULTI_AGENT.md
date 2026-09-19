# Launch Guard multi-agent foundation (v0.22.0)

This release provides a non-executing foundation for three logical agents using one future model/API connection:

1. **Opportunity Hunter** proposes new token candidates.
2. **Portfolio Manager** proposes hold, partial-sale, and exit decisions for owned positions.
3. **Copy Trader** discovers candidate leader wallets and may propose attributed copies. It is not tied to a fixed trader list and cannot submit an unattributed copy buy.

## Safety boundary

Agents produce structured `TradeProposal` objects. `RiskArbiter` is deterministic and is the only component that approves a proposal for downstream handling. Its v0.22 defaults allow only `paper` and `shadow` modes. A model cannot change policy limits in its response.

Default agent limits preserve the existing conservative envelope: $5 maximum order, two open positions, 3% position cap, 3% daily loss cap, $5,000 minimum liquidity, 5% maximum quoted price impact, 15-second quote age, 0.65 minimum confidence, and a kill switch.

This module does not hold an API key, private key, wallet credential, transaction signer, or live execution adapter. Connecting an OpenAI API later should implement the small `AgentModel` protocol and return structured dictionaries; doing so does not enable live execution.

## Performance and survival

Each closed paper trade is attributed to exactly one agent. Daily reports show net P&L after recorded fees, return on deployed capital, wins/losses, and drawdown per agent and in total.

Weekly survival evaluation has two gates before an agent can be judged: at least 28 days old and at least 30 completed trades. Poor performance first causes probation. Retirement requires severe or persistent poor risk-adjusted results. Retired histories should be preserved; replacement agents should receive a new generation and ID.

These rules reduce one-week luck and reckless risk taking, but cannot establish that a strategy will be profitable.

## Learning from professional videos

The learning module accepts a transcript only with an attributable HTTPS URL, title, and author. A strategy lesson must quote an excerpt actually present in that transcript and specify entry, exit, invalidation, and risk rules.

Every video-derived lesson begins as `RESEARCH_ONLY`. Promotion requires at least 50 paper trades, 30 forward-test days, positive net return after fees and slippage, and maximum drawdown no greater than 12%. A lesson that passes these gates is still eligible only for a later shadow-mode review; it never directly activates live trading.

Use videos only when the uploader has made them lawfully available and transcription/use complies with the platform terms and applicable rights. Store citations and extracted rules, not downloaded copyrighted video files.

## Current scope

v0.22 is intentionally the safe foundation. It adds the data contracts, guardrails, reporting math, survival policy, and research-promotion policy. Model API calls, trader-data discovery adapters, video transcript providers, scheduler wiring, UI, and transaction execution are later integrations and remain disabled/absent.


## OpenAI connection (v0.24.0)

One OpenAI client powers the three logical roles through the Responses API. The adapter requests schema-validated proposals, sends requests with `store=False`, removes credential-like fields from nested context, limits prompt field sizes, and treats market/wallet content as untrusted data.

After pulling and reinstalling, test the key using synthetic data only:

```bash
git pull --ff-only origin main
.venv/bin/pip install -e ".[dev]"
.venv/bin/launch-guard-agents --test-api
```

Expected JSON includes `"connected": true`, `"action": "HOLD"`, and `"live_execution": false`. The test does not read a wallet, propose a real token, sign, simulate, or broadcast a transaction.

To run all three roles against synthetic examples:

```bash
.venv/bin/launch-guard-agents --paper-demo
```

The paper demo can approve a bounded hypothetical proposal in its output, but has no execution adapter and cannot submit it. Real market/trader/portfolio wiring remains a separate shadow-mode phase.


## Three $30 shadow accounts (v0.25.0)

Initialize the three isolated agent accounts once:

```bash
.venv/bin/launch-guard-agents --initialize-capital 30
```

This creates $30 each for `hunter-v1`, `portfolio-v1`, and `copy-v1` ($90 total virtual capital). Initialization is idempotent and refuses to overwrite an existing book with a different allocation.

Each shadow buy is capped at $5 and each agent may have at most two open shadow positions, so at most $10 of each $30 account can be committed simultaneously. Accounts cannot borrow from one another. View balances with:

```bash
.venv/bin/launch-guard-agents --capital-status
```

After the regular Launch Guard monitor has written fresh recommendation and portfolio snapshots, request one real-data shadow decision from each agent:

```bash
.venv/bin/launch-guard-agents --shadow-once
```

The Opportunity Hunter reads the highest-ranked recommendation, the Portfolio Manager reads the highest-priority owned-position signal, and the Copy Trader reads `AGENT_COPY_SIGNAL_PATH` when a leader-data adapter supplies it. Missing or stale inputs fail closed through the arbiter. Approved BUY proposals create virtual positions only. Decisions are appended locally to `AGENT_DECISION_LOG_PATH`.

This release does not connect agent approval to Jupiter, a signer, or live execution. The capital file explicitly records `live_execution: false`.

## One-time supervised live canary

Version 0.26 adds a one-attempt, $1 USDC mainnet canary. It is disabled by default and does not make continuous agent execution live. Before any broadcast it requires a fresh BUY NOW/BUY ZONE recommendation, at least $50,000 liquidity, complete entry confirmations, a matching Keychain signer, sufficient USDC, no existing holding in the mint, live exit protection, price impact at or below 3%, slippage at or below 300 bps, and a successful RPC simulation.

Keep `AGENT_LIVE_KILL_SWITCH=true` except during the supervised test. First run `launch-guard-agents --live-test-preflight`. Only after reviewing that output, run `launch-guard-agents --live-test-execute --confirm SPEND_1_USDC_ON_MAINNET`. A local journal makes the canary one-attempt-only and blocks blind retry after an uncertain outcome.
