# Robinhood wallet setup (not live trading)

Install the optional dependency with `python -m pip install -e '.[robinhood]'`.
Keep the public address in `EVM_WALLET_ADDRESS`. A distinct address can be set
in `ROBINHOOD_WALLET_ADDRESS` for these setup commands only; the existing
multichain monitor still uses EVM_WALLET_ADDRESS.

On macOS run:

```sh
.venv/bin/launch-guard-robinhood --import-signer
.venv/bin/launch-guard-robinhood --verify-signer
.venv/bin/launch-guard-robinhood --check-wallet
```

Import asks for the exported private key at a hidden terminal prompt, checks
its derived address against the configured public wallet, and stores it in
native macOS Keychain. It refuses piped input and existing entries. No key
belongs in .env, command arguments, chat, or repository files. Keychain access
may prompt locally. Verification proves an address match, not swap readiness.

Add `--token CONTRACT_ADDRESS` to --check-wallet to read an ERC-20 raw balance
and decimals at the same block as the native balance. It does not load the key.
If the public RPC times out, set ROBINHOOD_RPC_URL to a working HTTPS provider
endpoint. Provider errors are redacted to avoid leaking endpoint credentials.

Every check returns live_execution=false. This module has no signing or
broadcast operation. It does not alter existing Solana settings or processes.
Contract/delegated wallet code is reported as a blocker for further review.
A nonzero gas balance does not establish that a swap is affordable.

Still required before Robinhood trading: validated swap encoding/routing,
quote and buy/sell simulations, gas/slippage/spending limits, nonce and retry
handling, receipt/balance reconciliation, and live agent budget integration.
The existing $30 shadow accounts are not live spend limits.

Network reference (chain ID 4663, native ETH):
https://docs.robinhood.com/chain/connecting/

Uniswap deployment reference (deployment alone does not verify a swap):
https://developers.uniswap.org/docs/protocols/v4/deployments

To inspect a contract/delegated Fomo wallet without broadcasting, run:

```sh
.venv/bin/launch-guard-robinhood --inspect-wallet
```

It reads contract code at a fixed block and makes a non-state-changing
EIP-1271 `eth_call` using a locally generated signature. It never prints the
private key and never signs or submits a transaction. A positive EIP-1271
result confirms only signature validation; it does not demonstrate that a swap,
gas sponsor, or account-abstraction submission will work.
