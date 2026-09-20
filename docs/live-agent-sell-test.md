# One real Portfolio-agent sell test

This command lets the Portfolio agent choose one full Solana exit from fresh
EXIT WARNING recommendations, or choose not to trade. It is not a shadow run.
It does not enable Robinhood, Hunter buys, rebuys, or the copy trader.

Stop any running live trading processes first. Keep a portfolio monitor running
with AUTO_BUY_LIVE=false and AUTO_SELL_LIVE=false so its snapshot stays fresh.
Start that monitor in a separate terminal with:

```sh
AUTO_BUY_LIVE=false AUTO_SELL_LIVE=false .venv/bin/launch-guard --mode portfolio
```

Run the one-shot agent from the same project directory:

```sh
AGENT_LIVE_TEST_ENABLED=true AGENT_LIVE_KILL_SWITCH=false AUTO_BUY_LIVE=false AUTO_SELL_LIVE=false .venv/bin/python -m solana_launch_guard.agent_live_sell --execute
```

The command's explicit execution path operates independently of the disabled
background auto-traders. Existing Solana signer and Jupiter credentials must
already be configured. This can sell an existing personal holding; it does not
sell an imaginary $30 shadow position.

Limits: one decision and at most one sale; full-position snapshot value $2-$5;
quote minimum at least the configured sell floor (never below $2); expected
USDC proceeds at most $5; maximum exact quote impact 3%; maximum slippage 300
bps. Network fees are additional and are not included in the $5 proceeds cap.
The agent's confidence threshold is 0.65, not a calibrated probability.

No eligible recommendation means no OpenAI request. Otherwise there is one
model invocation with SDK retries disabled. A fresh matching recommendation
must still exist after model latency and again after sell simulation. Wallet
balance must match before submission. The existing executor durably claims
the sale, and confirmed transaction token deltas are checked afterward.

The session writes launch_guard_agent_sell_session.json and a .claim file.
After any model decision the session cannot automatically run again, including
HOLD, errors, or uncertain receipts. Do not delete journals to force retries;
review the transaction and wallet first. A verification failure may occur after
a successful sale, so STOPPED_REVIEW_REQUIRED is not proof that no trade occurred.

Tests use mocked execution. A real wallet run is still required to validate
provider, signer, transaction, and confirmation behavior together.
