# Solana Launch Guard

A safety-first Python monitor and paper trader for newly created Pump.fun tokens.

Version 0.5 **never signs or submits transactions**. It can monitor new-token events or a public Solana trader wallet, apply configurable gates, rank intelligence-qualified paper candidates, open simulated positions, follow prices through a public market-data endpoint, and record decisions and P&L in SQLite.

## What it does

- Receives real-time new-token events from PumpPortal.\n- Watches public trader wallets through Solana RPC without requiring wallet credentials.\n- Records wallet buys/sells and can paper-copy qualifying buys.
- Rejects launches that lack enough pricing data or violate configured limits.
- Scores observed launches with separate safety and momentum components.\n- Uses a CORE tier for stronger setups and a $5 MOONSHOT tier for higher-risk setups.\n- Limits position size, concurrent positions, and total exposure.
- Prints a gold top-10 paper watchlist and continuously re-ranks it using bounded live price momentum.
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
| `RECOMMENDATION_LIMIT` | `10` | Number of ranked paper candidates shown, from 1 through 10 |
| `RECOMMENDATION_POOL_SIZE` | `30` | Qualified candidates retained for live ranking |
| `RECOMMENDATION_POLL_SECONDS` | `15` | Seconds between quote refreshes and ranking updates |
| `RECOMMENDATION_TTL_SECONDS` | `1800` | Seconds before a candidate ages out of the watchlist |
| `COLOR_OUTPUT` | `true` | Use gold ANSI terminal output when supported |

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


## Intelligent launch filter

New launches are no longer bought immediately. After passing the event-level
prefilter, each token enters a configurable observation window. The scorer then
uses DEX Screener's five-minute transaction, volume, price-change, liquidity,
market-cap, and pair data.

| Tier | Default paper size | Minimum profile | Default exit |
| --- | ---: | --- | --- |
| CORE | $10 equivalent | score 75+, safety 35+, liquidity $20K+ | +30% / -20% |
| MOONSHOT | $5 equivalent | score 60+, safety 25+, market cap <= $500K | +5000% / -40% |
| REJECT | $0 | hard gate or score not met | none |

The 5,000% moonshot target is an experiment, not a forecast. A +5,000% gain
would turn $5 into $255 before fees because final value is
$5 × (1 + 50) = $255. Real execution would also face slippage, fees, failed
transactions, and the possibility of a total loss.

Each scoring result is stored in the `intelligence_scores` SQLite table with
its component scores and reasons, allowing thresholds to be calibrated from
paper results instead of intuition.

### Live recommended-paper-buy watchlist

When launch monitoring is active, every CORE or MOONSHOT result enters an
in-memory watchlist even when the paper portfolio already has three open
positions. Every 15 seconds the bot refreshes current quotes and prints up to
10 gold rows with the rank, tier, signal score, price rise since qualification,
five-minute change, liquidity, current price, and exact mint.

The live signal score keeps the original intelligence score as the main input.
The rise-since-observation and five-minute price inputs are capped before they
are added, so a brief extreme pump cannot dominate the ranking solely because
of its percentage increase. The list expires candidates after 30 minutes by
default and starts fresh when the process restarts.

Use both launch scanning and public-wallet monitoring together:

```bash
launch-guard --mode both
```

The label is a paper-trading model signal, not a statement that a token will
rise. Rankings can reverse quickly, and the bot does not submit a real order.


## Adaptive exits and re-entry

Open paper positions are checked against both their configured hard stop/target and
an adaptive strategy:

- trailing protection activates after a 20% peak gain and exits after a 12%
  pullback from the peak;
- a five-minute move of -8% or worse can exit when sell pressure is at least
  1.5 times buy activity and at least five sellers are observed;
- a 30% liquidity reduction can exit when sellers also outnumber buyers;
- re-entry waits at least 120 seconds, requires positive five-minute momentum,
  buyer dominance, a rising latest tick, and at least 80% of baseline liquidity;
- at most two re-entries are permitted per token during one process session.

These are heuristic paper rules, not price predictions. DEX Screener data can be
delayed or incomplete, and a sudden gap or rug can move faster than polling.

### Import an existing Solana Fomo holding

Use the token mint, the quantity you still hold, and the USD cost basis allocated
to that remaining quantity:

```bash
launch-guard \
  --import-fomo-mint TOKEN_MINT \
  --import-symbol SYMBOL \
  --import-token-amount 123456.78 \
  --import-cost-usd 25
```

Then monitor it in launch mode without opening a new database:

```bash
launch-guard --mode launches
```

Importing creates a paper representation only. It does not connect to Fomo,
submit an order, or move the real holding. Never put a Fomo password, session
cookie, recovery phrase, or private key in this project.
