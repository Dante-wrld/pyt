# Solana Launch Guard

A safety-first Python monitor and paper trader for newly created Pump.fun tokens.

Version 0.2 **never signs or submits transactions**. It can monitor new-token events or a public Solana trader wallet, apply configurable gates, open simulated positions, follow prices through a public market-data endpoint, and record decisions and P&L in SQLite.

## What it does

- Receives real-time new-token events from PumpPortal.\n- Watches public trader wallets through Solana RPC without requiring wallet credentials.\n- Records wallet buys/sells and can paper-copy qualifying buys.
- Rejects launches that lack enough pricing data or violate configured limits.
- Limits position size, concurrent positions, and total exposure.
- Simulates take-profit and stop-loss exits using subsequent trade events.
- Persists launches, decisions, positions, and fills in `launch_guard.db`.
- Reconnects after WebSocket failures.
- Keeps live execution deliberately absent from this version.

## Requirements

- Python 3.11 or newer
- A PumpPortal API key if their current access policy requires one

PumpPortal's current documentation describes `subscribeNewToken` as the new-token stream and `subscribeTokenTrade` as the per-token trade stream. The endpoint and key are configuration values so they can be changed without editing the code.

## Quick start

```bash
git clone https://github.com/Dante-wrld/pyt.git
cd pyt

python -m venv .venv

# Windows PowerShell
.venv\Scripts\Activate.ps1

# macOS/Linux
source .venv/bin/activate

pip install -e ".[dev]"
copy .env.example .env
```

On macOS/Linux, use `cp .env.example .env`.

Edit `.env`, then run:

```bash
launch-guard
```

For a deterministic offline demonstration that makes no network connection:

```bash
launch-guard --demo
```

Run tests:

```bash
pytest
```

## Configuration

| Variable | Default | Meaning |
|---|---:|---|
| `PUMPPORTAL_WS_URL` | `wss://pumpportal.fun/api/data` | WebSocket endpoint |
| `PUMPPORTAL_API_KEY` | empty | Appended as `api-key` when supplied |
| `PAPER_TRADE_SIZE_SOL` | `0.02` | Simulated amount per accepted launch |
| `MAX_OPEN_POSITIONS` | `3` | Concurrent position limit |
| `MAX_TOTAL_EXPOSURE_SOL` | `0.06` | Portfolio exposure cap |
| `MIN_VIRTUAL_SOL` | `5` | Minimum virtual SOL reserve reported by the event |
| `MIN_MARKET_CAP_SOL` | `5` | Minimum market cap in SOL |
| `MAX_MARKET_CAP_SOL` | `500` | Maximum market cap in SOL |
| `MAX_CREATOR_BUY_SOL` | `5` | Maximum creator SOL amount reported at launch |
| `TAKE_PROFIT_PCT` | `30` | Paper exit above entry |
| `STOP_LOSS_PCT` | `20` | Paper exit below entry |
| `DATABASE_PATH` | `launch_guard.db` | SQLite path |
| `SOLANA_RPC_HTTP_URL` | public mainnet RPC | Transaction lookup endpoint |
| `SOLANA_RPC_WS_URL` | public mainnet WebSocket | Wallet log subscription endpoint |
| `WATCHED_WALLETS` | empty | Comma-separated public wallets |
| `PRICE_POLL_SECONDS` | `5` | Seconds between paper-position price checks |
| `COPY_MIN_LIQUIDITY_USD` | `10000` | Minimum liquidity for a copied paper entry |
| `REJECT_UNKNOWN_PRICE` | `true` | Reject launches without a calculable price |

The defaults are engineering examples, **not financial recommendations**. They should be evaluated in paper mode over a meaningful sample before any live-execution module is considered.

## Price model

The listener calculates an indicative token price from the event's virtual reserves:

```text
price_sol_per_token = vSolInBondingCurve / vTokensInBondingCurve
paper_quantity      = PAPER_TRADE_SIZE_SOL / price_sol_per_token
PnL %               = (latest_price / entry_price - 1) × 100
```

Pump.fun events may represent token amounts in different unit scales. Because both entry and later marks use the same reserve-based method when available, relative P&L remains the useful paper-testing signal. The simulator stores the raw event JSON for auditing.

## Important limitations

- A passing result means only that the launch passed the configured event-level checks. It does **not** prove the token is safe.
- This release does not claim to verify mint authority, freeze authority, holder concentration, bundled supply, social authenticity, or liquidity lock status. Those require additional on-chain or indexed data.
- WebSocket feeds can be delayed, incomplete, changed, rate-limited, or unavailable. Public Solana RPC is suitable for paper testing but not guaranteed low-latency production copying.\n- DEX Screener may not index a brand-new pair immediately, so some price marks or copy entries can be delayed or skipped.
- Paper fills ignore latency, slippage, price impact, priority fees, platform fees, failed transactions, and MEV.
- New tokens can lose essentially all value.

## Next release gate

Live execution should be added only after:

1. Paper results are reviewed from the SQLite database.
2. On-chain authority and holder-concentration enrichment is implemented.
3. Slippage, fee, stale-price, cooldown, daily-loss, and kill-switch limits are tested.
4. A dedicated low-balance wallet is used.
5. A manual arming step is required at every startup.

Official references:

- [PumpPortal real-time data](https://pumpportal.fun/data-api/real-time/)
- [Solana token verification guidance](https://solana.com/docs/tokens/how-to-verify-a-token)
