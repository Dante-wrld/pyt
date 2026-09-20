# Bounded Solana overnight canary

Run this on the Mac that holds the Solana signer and `.env`. It monitors launches
and your wallet for up to eight hours. It waits for a fresh, three-confirmation
Solana signal with at least $50,000 reported liquidity, locally preflights a
fixed $5 USDC purchase and a reverse sale quote, and may broadcast **one** buy.
Once acquired, the existing portfolio monitor may sell **only that confirmed
canary mint** if its configured signal and sell checks pass. A sale is not
guaranteed; the position may remain in the wallet at the end of eight hours.
The command stops its own monitor at the deadline or on Ctrl+C.

This is a deterministic canary, not a live AI Hunter or Portfolio model. It
cannot spend the separate $30 shadow-agent balances or repeat a buy after a
claimed attempt. The copy agent and Robinhood trading are not enabled.

Before starting, stop other Launch Guard processes that use this wallet and
database. On the Mac, check that `.env` contains one active value for each:

```
AGENT_LIVE_TEST_ENABLED=true
AGENT_LIVE_KILL_SWITCH=false
AGENT_LIVE_TEST_AMOUNT_USD=5
AGENT_LIVE_CANARY_ONLY=true
AUTO_SELL_ENABLED=true
AUTO_SELL_LIVE=true
AUTO_SELL_PORTFOLIO_SIGNALS=true
AUTO_BUY_LIVE=false
AUTO_REBUY_ENABLED=false
```

The existing SOLANA_WALLET_ADDRESS and JUPITER_API_KEY are required. The
runner does not edit `.env`, import a signer, or enable a flag. It refuses to
run if the previous one-time canary journal or its claim exists. Keep the Mac
awake, connected, and able to access its keychain. From the project directory:

```sh
.venv/bin/python -m solana_launch_guard.overnight_trial --hours 8
```

Watch the terminal and wallet history. To stop the trial, press Ctrl+C in that
terminal. After a buy, inspect the canary journal and the wallet: loss of
connectivity, an uncertain receipt, or a failed exit requires manual review.
Do not delete the canary journal or its `.claim` file to force another buy.
