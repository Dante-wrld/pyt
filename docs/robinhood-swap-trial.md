# Robinhood one-shot swap trial

This module adds a real, explicitly invoked buy/sell execution path. It does
**not** turn the Hunter or Portfolio shadow loop into an autonomous live loop.
It requires Robinhood Chain native ETH in the locally configured wallet, for
both gas and native-ETH buys. Fomo's displayed cash is not a native gas balance.

## Update and preflight

```sh
cd ~/pyt
git pull --ff-only origin main
.venv/bin/pip install -e '.[robinhood]'
.venv/bin/python -m solana_launch_guard.robinhood_swap --token TOKEN_ADDRESS --side buy --usd 3
```

Replace TOKEN_ADDRESS with the public Robinhood ERC-20 address. For a sell,
use `--side sell`; it plans a partial sale worth approximately the requested USD
amount, not a full position liquidation. No `--execute` means no broadcast,
including no approval transaction. Sells need the locally stored signer to
construct a short-lived Permit2 signature for simulation. No key is printed.

Preflight reports quote amounts, whether a token approval is needed, and whether
the actual router transaction passed `eth_call` and gas estimation. Missing
approval prevents full sell simulation until that approval is mined. Discovery
errors or missing gas stop the trial. A preflight result is not a trade signal.

## One real trial

Use only after reviewing preflight and funding native ETH on **chain 4663**.
The amount is bounded to $2–$5, plus gas; default $3. Stop other transactions
from this wallet during the trial so balance changes can be reconciled.

```sh
ROBINHOOD_LIVE_TEST_ENABLED=true AGENT_LIVE_KILL_SWITCH=false \
.venv/bin/python -m solana_launch_guard.robinhood_swap --token TOKEN_ADDRESS --side buy --usd 3 --execute
```

For a sell, change `--side buy` to `--side sell`. This can broadcast an exact
ERC-20 approval followed by the swap. No unlimited approval is created. An
existing insufficient nonzero allowance is rejected rather than reset silently.
If anything fails after approval, that limited approval can remain; inspect the
journal. The Permit2 router allowance is exact and expires after 90 seconds.

Only **one execution attempt total** is permitted by
`launch_guard_robinhood_trial.json` and its `.claim` lock. Do not delete either
to force another trade after a timeout. The transaction hash is fsynced before
submission, and submission is never automatically retried. Review the recorded
hash and balances before authorizing any subsequent trial. Preflight can run
repeatedly without consuming the execution attempt.

## Enforced scope

- Robinhood chain ID 4663; EOA or EIP-7702 delegated EOA with matching local key.
- Fixed Uniswap UniversalRouter 2.1.2 address and PoolManager; runtime length and
  router/quoter PoolManager checks, not a full bytecode equivalence proof.
- Native ETH/token v4 pools, sorted currencies, no hooks, static fee up to 1%.
- PoolKey recovered from a bounded scan of actual PoolManager Initialize logs;
  hash must equal the discovered pool ID. No guessed fees or hook addresses.
- At least $50,000 reported pool liquidity; public discovery data refreshed
  before sizing. Market-data availability/accuracy is not guaranteed.
- Quote within 3% of market valuation; 1% minimum-output slippage limit;
  reverse buy quote recovers at least 95% before gas. This is not a simulated
  sell of the newly acquired tokens and cannot establish future sellability.
- $2 minimum quoted sell proceeds after slippage; $5 maximum input valuation.
- Gas reservation across approval and swap capped at both 0.0001 ETH and $1
  at the discovered native-ETH price. Provider fee behavior can change.
- Fresh quote, pending-nonce check, simulation, gas estimation, deadline,
  exact approval, and durable hash before broadcast. Any ambiguous outcome stops.
- Mined receipt sender/recipient/status checks, token transfer checks, canonical
  receipt block check, and before/after token/native balances recorded. Mined
  is not finality; concurrent wallet activity can affect balance differences.

Unsupported pools, unavailable history, no gas, or failed simulation remain
blocked. This is deliberately a supervised execution test, not a claim that
all Fomo/Robinhood tokens or continuous agent trading are ready.

## Pinned upstream interface

Router tag `2.1.2` resolves to commit
`802fe4c18f47300e0f183e2a42e9146ec2ea9fc3`. Its **git submodule** for
v4-periphery is `545a5d2a87228167edde48f3b9eda122d1e3c4d6`.
`foundry.lock` points to an older revision and must not be used as the ABI source.
`ExactInputSingleParams` includes `minHopPriceX36`; omitting it misencodes the swap.

- [Release source](https://github.com/Uniswap/universal-router/tree/802fe4c18f47300e0f183e2a42e9146ec2ea9fc3)
- [Deployed Robinhood addresses](https://github.com/Uniswap/universal-router/blob/64027f3372aa235734207c4e05667204f6f927e1/deploy-addresses/robinhood.json)
- [Exact router interface](https://github.com/Uniswap/v4-periphery/blob/545a5d2a87228167edde48f3b9eda122d1e3c4d6/src/interfaces/IV4Router.sol)
- [Official v4 deployments](https://developers.uniswap.org/docs/protocols/v4/deployments)

Validation in development uses mocked RPC and locally generated test keys. A
real wallet simulation and a successful on-chain trade still require the user's
local RPC, signer, gas, and eligible pool. No real transaction was sent during
implementation.
