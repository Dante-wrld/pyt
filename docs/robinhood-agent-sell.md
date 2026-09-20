# Robinhood Portfolio-agent sell trial

This one-decision command gives the Portfolio agent a bounded choice to HOLD or
sell part of an existing Robinhood Chain token position. Specify the token's
public contract address and fixed sale value. The agent does not select a
different token, enlarge the amount, or place a buy. The wallet's actual token
balance is read from chain. Missing entry price means the agent cannot assert
net profit, and its confidence is not a measured chance of profit.

Install the optional Robinhood dependencies and run a read-only agent review:

```sh
cd ~/pyt
git pull --ff-only origin main
.venv/bin/pip install -e '.[robinhood]'
.venv/bin/python -m solana_launch_guard.robinhood_agent_sell --token TOKEN_ADDRESS --usd 3
```

The preflight checks the chain, signer, owned balance, verified Uniswap pool,
fresh exact sell quote, gas balance, and simulation of the router swap (or exact
token approval when that approval has not yet been mined). It calls the model
only after these checks succeed. A sell requiring approval cannot pass the
router simulation until after the exact approval is mined. No funds move during
the read-only review.

One explicitly supervised live attempt, after reviewing the read-only result:

```sh
ROBINHOOD_LIVE_TEST_ENABLED=true AGENT_LIVE_KILL_SWITCH=false \
.venv/bin/python -m solana_launch_guard.robinhood_agent_sell --token TOKEN_ADDRESS --usd 3 --execute
```

The live attempt repeats on-chain checks and quotes. The existing swap executor
limits the order to $2–$5, enforces minimum proceeds, permits at most an exact
approval, simulates before broadcasting, and records the signed hash before
submission. It will not route unsupported or unquotable pools. It needs native
ETH on Robinhood Chain to pay gas, even for a token sale. It uses both a
single-decision agent session journal and the existing single-attempt swap
journal; never delete or reset either after an ambiguous outcome. This is one
token and one decision, not background portfolio monitoring. See
[swap trial](robinhood-swap-trial.md) for execution limits.

Do not paste the private key, seed phrase, or RPC URL into chat. Keep the
existing local macOS Keychain signer.
