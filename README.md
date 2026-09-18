# Launch Guard

A safety-first Python monitor, paper trader, and opt-in guarded Solana
buyer/automatic seller for multichain crypto tokens.

Launch Guard can automatically select fresh, confirmed Solana `BUY NOW`/`BUY
ZONE` candidates and optionally execute wallet-wide `TAKE PARTIAL`, `PROTECT
PROFIT`, and `EXIT WARNING` rules. Version 0.20.1 preserves Jupiter's
`lastValidBlockHeight` as the string required by the `/execute` schema. Version
0.20 added sanitized Jupiter error evidence, public-signature preservation, and
an explicit reconcile-pause-resume workflow for frozen sell batches. Adaptive
wallet-exit chunks can halve an unsafe quote without weakening either guard,
and confirmed progress persists across polling cycles and restarts. Every
unattended order must pass a Jupiter price-impact cap, a separate slippage cap,
local signing, and Solana RPC simulation before broadcast. Principal remains
limited to two $5 USDC seed purchases and two active bot-managed positions.
Direct on-chain swaps use Jupiter; Launch Guard does not log in to or control
Fomo's app or website.

## What it does

- Receives real-time new-token events from PumpPortal.
- Watches public trader wallets through Solana RPC without requiring wallet credentials.
- Records wallet buys/sells and can paper-copy qualifying buys.
- Rejects launches that lack enough pricing data or violate configured limits.
- Scores observed launches with separate safety and momentum components.
- Uses a CORE tier for stronger setups and a $5 MOONSHOT tier for higher-risk setups.
- Limits position size, concurrent positions, and total exposure.
- Prints a gold top-10 paper watchlist and continuously re-ranks it using bounded live price momentum.
- Classifies qualified tokens as `ENTRY PENDING`, `BUY NOW`,
  `WAIT FOR PULLBACK`, `PULLBACK STARTED`, `BUY ZONE`, `WATCH`, or `AVOID`.
- Requires repeated entry confirmation and blocks entries when live score,
  liquidity retention, or volume quality deteriorates.
- Optionally sends explainable, state-change-deduplicated Pushover alerts to
  your phone without connecting to a trading account.
- Discovers crypto-token profiles across seven EVM networks and prints the chain,
  USD price, exact `0x` contract, and DEX Screener market link.
- Filters official and recognizable wrapped stock tokens from recommendations.
- Watches ERC-20 transfers for one shared public EVM address and separately
  reads HyperCore spot balances, perpetual positions, and fills.
- Reads non-zero SPL-token balances from one public Solana wallet and opens a
  separate holdings board with `HOLD`, `TAKE PARTIAL`, `PROTECT PROFIT`,
  `EXIT WARNING`, or `UNPRICED` guidance.
- Sends optional state-change-deduplicated, high-priority Pushover alerts for
  confirmed entries and configured profit-protection/exit states.
- Can dry-run or execute a two-stage, per-token-armed Solana profit ladder:
  recover the original USD principal at 2x, then sell half the remainder at 3x.
- Can apply deterministic wallet-wide sell rules: sell 50% on `TAKE PARTIAL`
  and 100% on `PROTECT PROFIT` or `EXIT WARNING`, with configurable fractions.
- Can preflight the next sale for one armed mint by building and locally
  signing the real Jupiter transaction, then simulating it without broadcasting.
- Can either allow-list one Solana mint or automatically select fresh,
  sufficiently liquid, confirmed `BUY NOW`/`BUY ZONE` candidates.
- Separates two $5 seed buys from a reinvestment pool containing only 50% of
  positive realized profit from bot-managed positions.
- Simulates take-profit and stop-loss exits using subsequent trade events.
- Persists launches, decisions, positions, fills, and sell execution state in
  `launch_guard.db`.
- Reconnects after WebSocket failures.
- Never uses SOL as an automated buy input. Portfolio guidance places orders
  only when `AUTO_SELL_PORTFOLIO_SIGNALS=true`; the default remains off.

## Requirements

- Python 3.11 or newer
- A PumpPortal API key if their current access policy requires one
- A Jupiter API key and operating-system keychain when preflighting or executing
  guarded swaps

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
| `SOLANA_WALLET_ADDRESS` | empty | Public Solana address used for balance discovery and live-signer verification |
| `WATCHED_WALLETS` | empty | Comma-separated public wallets |
| `PRICE_POLL_SECONDS` | `5` | Seconds between paper-position price checks |
| `COPY_MIN_LIQUIDITY_USD` | `10000` | Minimum liquidity for a copied paper entry |
| `REJECT_UNKNOWN_PRICE` | `true` | Reject launches without a calculable price |
| `RECOMMENDATION_LIMIT` | `10` | Number of ranked paper candidates shown, from 1 through 10 |
| `RECOMMENDATION_POOL_SIZE` | `30` | Qualified candidates retained for live ranking |
| `RECOMMENDATION_POLL_SECONDS` | `15` | Seconds between quote refreshes and ranking updates |
| `RECOMMENDATION_TTL_SECONDS` | `1800` | Seconds before a candidate ages out of the watchlist |
| `PULLBACK_TRIGGER_PCT` | `8` | Rise or five-minute move that anchors a pullback zone |
| `PULLBACK_ZONE_MIN_PCT` | `4` | Shallow edge of the preferred pullback range |
| `PULLBACK_ZONE_MAX_PCT` | `6` | Deep edge of the preferred pullback range |
| `PULLBACK_STARTED_PCT` | `2` | Drop from the tracked peak that changes the state to `PULLBACK STARTED` |
| `ENTRY_CONFIRMATION_POLLS` | `3` | Consecutive qualifying polls required before a final entry signal |
| `ENTRY_MIN_SIGNAL_SCORE` | `65` | Minimum live score during entry confirmation |
| `ENTRY_MIN_LIQUIDITY_RETENTION_PCT` | `80` | Minimum percentage of observed liquidity that must remain |
| `ENTRY_REQUIRE_NONFALLING_VOLUME` | `true` | Block final entry signals while five-minute volume is falling |
| `MIN_ENTRY_REWARD_RISK_RATIO` | `2` | Minimum paper objective relative to the configured stop distance |
| `BUY_NOW_MIN_RATIO` | `1.2` | Minimum five-minute buyer/seller ratio for `BUY NOW` |
| `AVOID_ENTRY_MOMENTUM_PCT` | `-8` | Falling five-minute move used by the adverse-entry gate |
| `AVOID_ENTRY_SELL_PRESSURE_RATIO` | `2` | Seller/buyer pressure required with falling momentum for `AVOID` |
| `PUSHOVER_ENABLED` | `false` | Enable optional phone notifications |
| `PUSHOVER_APP_TOKEN` | empty | Private 30-character token for your Pushover application |
| `PUSHOVER_USER_KEY` | empty | Private 30-character Pushover user key |
| `PUSHOVER_DEVICE` | empty | Optional device name; empty sends to all your Pushover devices |
| `PUSHOVER_ALERT_DECISIONS` | `BUY NOW,BUY ZONE,PULLBACK STARTED,WAIT FOR PULLBACK,AVOID` | Decision changes that may notify |
| `PUSHOVER_MIN_SCORE` | `60` | Minimum live signal score required for phone alerts |
| `PUSHOVER_COOLDOWN_SECONDS` | `300` | Minimum delay between different alerts for one token |
| `PUSHOVER_PORTFOLIO_ALERT_DECISIONS` | `TAKE PARTIAL,PROTECT PROFIT,EXIT WARNING` | Owned-holding states that may notify |
| `PUSHOVER_HIGH_PRIORITY_DECISIONS` | `BUY NOW,BUY ZONE,TAKE PARTIAL,PROTECT PROFIT,EXIT WARNING` | States sent with Pushover priority 1 |
| `PUSHOVER_PORTFOLIO_COOLDOWN_SECONDS` | `300` | Minimum delay between holding alerts for one token |
| `COLOR_OUTPUT` | `true` | Use gold ANSI terminal output when supported |
| `PORTFOLIO_POLL_SECONDS` | `15` | Seconds between holdings checks (minimum 10) |
| `PORTFOLIO_MIN_VALUE_USD` | `0.01` | Hide priced wallet dust below this estimated USD value |
| `PORTFOLIO_SNAPSHOT_PATH` | `launch_guard_portfolio.json` | Local snapshot used by the holdings window |
| `AUTO_TRADE_FLOOR_PERCENTAGES` | `false` | Opt in to truncating quote percentages to whole percentage points before display and enforcement |
| `AUTO_SELL_ENABLED` | `false` | Evaluate armed 2x/3x ladder events; stays dry-run unless live mode is also enabled |
| `AUTO_SELL_LIVE` | `false` | Permit locally signed Jupiter sell submission for armed tokens |
| `AUTO_SELL_PRINCIPAL_MULTIPLE` | `2.0` | Entry-price multiple that triggers principal recovery |
| `AUTO_SELL_HALF_PROFIT_MULTIPLE` | `3.0` | Entry-price multiple that triggers the second stage |
| `AUTO_SELL_SECOND_STAGE_FRACTION` | `0.5` | Fraction of the remaining token balance sold at the second stage |
| `AUTO_SELL_MAX_PRICE_IMPACT_PCT` | `5.0` | Reject a Jupiter order above this reported price impact |
| `AUTO_SELL_MAX_SLIPPAGE_BPS` | `500` | Reject sell quotes whose reported or output-threshold slippage exceeds 5% |
| `AUTO_SELL_ADAPTIVE_CHUNKS` | `false` | Allow wallet-wide signal exits to halve an unsafe amount and continue a persisted target over later polls |
| `AUTO_SELL_MIN_CHUNK_FRACTION` | `0.01` | Smallest adaptive chunk as a fraction of the current token balance |
| `AUTO_SELL_MAX_CHUNK_ATTEMPTS` | `8` | Maximum guarded quote sizes tried during one polling cycle |
| `AUTO_SELL_PORTFOLIO_SIGNALS` | `false` | Permit owned-wallet `TAKE PARTIAL`, `PROTECT PROFIT`, and `EXIT WARNING` rules in dry-run/live mode |
| `AUTO_SELL_TAKE_PARTIAL_FRACTION` | `0.5` | Fraction sold once for `TAKE PARTIAL` |
| `AUTO_SELL_PROTECT_PROFIT_FRACTION` | `1.0` | Fraction sold once for `PROTECT PROFIT` |
| `AUTO_SELL_EXIT_WARNING_FRACTION` | `1.0` | Fraction sold once for `EXIT WARNING` |
| `AUTO_SELL_MIN_VALUE_USD` | `1.0` | Minimum priced wallet holding eligible for portfolio-signal execution |
| `AUTO_SELL_EXCLUDED_MINTS` | USDC mint | Exact Solana mints never sold by wallet-wide rules |
| `AUTO_BUY_ENABLED` | `false` | Evaluate guarded Solana candidates; remains dry-run unless live mode is also enabled |
| `AUTO_BUY_LIVE` | `false` | Permit locally signed Jupiter USDC purchases; requires live auto-selling |
| `AUTO_BUY_DISCOVERY` | `false` | Automatically create a one-shot policy for qualified Solana buy signals |
| `AUTO_BUY_DISCOVERY_MIN_SCORE` | `70` | Minimum live signal score for automatic selection |
| `AUTO_BUY_DISCOVERY_MIN_LIQUIDITY_USD` | `50000` | Minimum quoted liquidity for automatic selection |
| `AUTO_BUY_SIGNAL_MAX_AGE_SECONDS` | `30` | Maximum age of an automatically selected signal |
| `AUTO_BUY_EXCLUDED_MINTS` | empty | Exact Solana mints never selected automatically |
| `AUTO_BUY_SEED_SIZE_USDC` | `5.0` | Maximum USDC principal for each initial seed purchase |
| `AUTO_BUY_MAX_SEED_BUYS` | `2` | Lifetime number of principal-funded purchases before only profit may be reused |
| `AUTO_BUY_MAX_OPEN_POSITIONS` | `2` | Maximum actively managed bot positions, including pending purchases |
| `AUTO_BUY_REINVEST_PROFIT_PCT` | `50.0` | Positive realized-profit share credited to the reinvestment pool |
| `AUTO_BUY_MAX_PRICE_IMPACT_PCT` | `5.0` | Reject a Jupiter buy order above this reported price impact |
| `AUTO_BUY_MAX_SLIPPAGE_BPS` | `500` | Reject buy quotes whose reported or output-threshold slippage exceeds 5% |
| `JUPITER_API_KEY` | empty | Private Jupiter API key required for live order and execution requests |
| `ROBINHOOD_TOKEN_ADDRESSES` | empty | Comma-separated Robinhood Chain `0x` contracts to monitor in addition to discovery |
| `ETHEREUM_TOKEN_ADDRESSES`, `BASE_TOKEN_ADDRESSES`, `BNB_TOKEN_ADDRESSES`, `BOB_TOKEN_ADDRESSES`, `MONAD_TOKEN_ADDRESSES`, `HYPEREVM_TOKEN_ADDRESSES` | empty | Exact contracts to monitor per chain |
| `MULTICHAIN_POLL_SECONDS` | `15` | Seconds between multichain discovery passes (minimum 15) |
| `EVM_WALLET_ADDRESS` | empty | Shared public `0x` address monitored across configured EVM RPCs |
| `HYPERLIQUID_ADDRESS` | `EVM_WALLET_ADDRESS` | Public address used for HyperCore balances and fills |
| `EVM_WALLET_POLL_SECONDS` | `10` | Seconds between public-wallet checks |
| `*_RPC_URL` | varies | Read-only JSON-RPC endpoint for each EVM network |

The defaults are engineering examples, **not financial recommendations**.
Keep automated buying and selling in dry-run while validating mints, cost
bases, quotes, and trigger behavior.

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
- WebSocket feeds can be delayed, incomplete, changed, rate-limited, or unavailable.
- Public Solana RPC is suitable for paper testing but not guaranteed low-latency production copying.
- DEX Screener may not index a brand-new pair immediately, so some price marks or copy entries can be delayed or skipped.
- Paper fills ignore latency, slippage, price impact, priority fees, platform fees, failed transactions, and MEV.
- Live quotes and transactions can fail, expire, be front-run, or produce a
  different result than a DEX Screener mark. A fast pump and reversal can occur
  between polling intervals.
- An exported Solana private key has authority over the entire wallet even
  though Launch Guard limits its own policy to armed ladder mints and eligible
  wallet-wide signals. A compromised computer, keychain, dependency, or
  transaction provider can put every asset in that wallet at risk.
- The process can act only while the computer is awake, online, and running.
- New tokens can lose essentially all value.

## Live sell guardrails

- Live execution is off by default and requires the relevant enable/live
  flags, a Jupiter API key, and a matching keychain signer. The profit ladder
  also requires a positive imported USD cost basis and a separate arm action;
  wallet-wide rules require their separate portfolio-signals flag.
- Stage 1 will not submit unless Jupiter's minimum quoted USDC output covers
  the original principal after quote slippage and fees.
- The seller rejects excessive reported price impact, records an idempotent
  claim before submission, and advances a stage only after Jupiter reports a
  confirmed success with a transaction signature.
- Any uncertain or failed execution is frozen for human review with no
  automatic retry, avoiding a duplicate sell when the first result is unknown.
- Jupiter HTTP failures retain only allow-listed, length-limited error fields,
  the numeric code, and any public transaction signature. Request payloads,
  signed transactions, and API keys are not written into the diagnostic.
- Review recovery first compares the current on-chain balance with the stored
  pre-execution balance. Resolution leaves the batch paused; reactivation is a
  separate explicit command and never broadcasts by itself.
- Native SOL, wrapped SOL, USDC, and USDT are excluded. Keep enough SOL in the
  wallet to pay network and priority fees.

Official references:

- [PumpPortal real-time data](https://pumpportal.fun/data-api/real-time/)
- [Solana token verification guidance](https://solana.com/docs/tokens/how-to-verify-a-token)
- [Robinhood Chain connection details](https://docs.robinhood.com/chain/connecting/)
- [Robinhood Chain Stock Token registry](https://api.robinhood.com/rhj/assets)
- [DEX Screener API reference](https://docs.dexscreener.com/api/reference)
- [BNB Smart Chain JSON-RPC endpoints](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/json-rpc-endpoint/)
- [Monad network information](https://docs.monad.xyz/developer-essentials/network-information)
- [Hyperliquid HyperEVM](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/hyperevm)
- [Hyperliquid Info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)
- [Pushover Message API](https://pushover.net/api)
- [Jupiter Swap order and execute API](https://developers.jup.ag/docs/swap/order-and-execute)
- [Fomo Terms of Service](https://fomo.family/terms)


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

### Separate macOS recommendation window

Use this command to keep scanner diagnostics in the original Terminal while a
second Terminal displays only the current recommendation board:

```bash
launch-guard --mode both --recommendations-window
```

The second window clears and redraws instead of appending repeated boards. Each
visible coin uses a different color, and duplicate mints or case-insensitive
duplicate symbols are reduced to the highest-ranked entry. It also shows the
number of candidates still waiting for evaluation. The window closes its live
display when the main scanner process stops.

### Separate holdings window

Put only your public Solana address in `.env`:

```dotenv
SOLANA_WALLET_ADDRESS=YOUR_PUBLIC_SOLANA_ADDRESS
```

Then start the scanner and the dedicated holdings board:

```bash
launch-guard --mode portfolio --portfolio-window
```

This mode watches holdings only; it does not scan for new recommendations. To
run both boards alongside every configured feed, use:

```bash
launch-guard --mode all --recommendations-window --portfolio-window
```

The holdings board reads current SPL Token and Token-2022 balances using
Solana JSON-RPC and obtains market data from DEX Screener. It is read-only by
default. When the live seller is explicitly configured, armed 2x/3x ladder
events can submit a real USDC sale. Holdings-board signals remain advisory
unless `AUTO_SELL_PORTFOLIO_SIGNALS=true` is also explicitly configured.

Market-risk exits can be evaluated without a cost basis. Profit/loss,
`TAKE PARTIAL`, and cost-based stop guidance require a known entry price. For
an existing Fomo/Solana holding, record the amount and total USD cost once:

```bash
launch-guard \
  --import-fomo-mint ACTUAL_MINT \
  --import-symbol SYMBOL \
  --import-token-amount ACTUAL_QUANTITY \
  --import-cost-usd TOTAL_USD_PAID
```

The import is a local cost-basis record; it does not connect to Fomo or move
tokens. Wallet tokens that were not imported remain visible with `P/L=n/a`.

### Automated sells for the current Fomo Solana wallet

This mode gives the local signer full authority for the Solana wallet whose
public address is in `SOLANA_WALLET_ADDRESS`. Launch Guard narrows its own trade
policy to imported/armed profit-ladder mints and optional portfolio-signal
rules, but private-key authority cannot be technically restricted to those
tokens. Native SOL is not an SPL-token input and USDC is excluded from
wallet-wide rules by default.

The default ladder for every armed mint is:

1. At 2x the imported entry price, sell enough tokens for Jupiter's minimum
   quoted output to recover the original USD cost in USDC. For example, a $100
   cost basis requires a minimum quoted output of at least 100 USDC; the token
   amount may be slightly more than half because of fees and slippage.
2. After stage 1 confirms, at 3x the imported entry price, sell 50% of the
   then-current remaining token balance to USDC. The Jupiter minimum output
   must also preserve at least the 3x entry-price valuation for that amount.
3. Leave the final remainder in the wallet. There is no third automatic sale.

If the balance or quote cannot recover the full principal, reported price
impact or effective slippage exceeds its configured cap, RPC simulation fails,
or Jupiter cannot build a transaction, no sale is submitted. A token that
jumps directly beyond 3x still completes the principal-recovery stage first,
then becomes eligible for stage 2 on a later poll.

`AUTO_TRADE_FLOOR_PERCENTAGES=true` deliberately loosens both buy and sell
guards: a quoted magnitude from 5.00% through 5.99% is evaluated and displayed
as 5%, and 599 basis points is evaluated and displayed as 500 basis points.
Preflight output retains the exact source values as
`quoted_price_impact_pct` and `quoted_slippage_bps`. The default remains
`false` so configured caps are exact unless this behavior is explicitly
enabled.

Set up one guarded token at a time:

1. Create a Jupiter API key in the [Jupiter developer portal](https://developers.jup.ag/portal).
2. Add the public Fomo Solana wallet address and dry-run settings to `.env`:

   ```dotenv
   SOLANA_WALLET_ADDRESS=YOUR_PUBLIC_SOLANA_ADDRESS
   AUTO_SELL_ENABLED=true
   AUTO_SELL_LIVE=false
   JUPITER_API_KEY=YOUR_JUPITER_API_KEY
   ```

3. Export the wallet's Solana private key in Fomo on your own device, then put
   it directly into the hidden local prompt. Never paste it into chat, `.env`,
   logs, screenshots, or GitHub:

   ```bash
   launch-guard --store-fomo-solana-key
   launch-guard --verify-auto-sell-signer
   ```

   The key is stored in the operating-system keychain only after its public key
   matches `SOLANA_WALLET_ADDRESS`.

4. Import the quantity currently held and the USD cost allocated to that
   remaining quantity, then arm its exact mint:

   ```bash
   launch-guard \
     --import-fomo-mint TOKEN_MINT \
     --import-symbol SYMBOL \
     --import-token-amount CURRENT_QUANTITY \
     --import-cost-usd ORIGINAL_USD_COST

   launch-guard --arm-auto-sell-mint TOKEN_MINT
   launch-guard --auto-sell-status
   ```

   Repeat the import and arm commands for each eligible current holding. This
   deliberate per-token step prevents an unknown airdrop or spam token from
   becoming executable merely because it appears in the wallet. If you trade
   the token manually before an automated stage, update the imported quantity
   and remaining cost basis so its entry price is still accurate.

5. Run dry-run mode and check that the board says `AUTO-SELL DRY RUN`:

   ```bash
   launch-guard --mode portfolio --portfolio-window
   ```

6. In another Terminal, preflight the armed mint. This requests a real Jupiter
   order, signs it locally, and sends it only to Solana's `simulateTransaction`
   RPC method. The preflight excludes JupiterZ because its RFQ transaction
   requires an additional market-maker signature supplied only during
   execution. It never calls Jupiter's execution endpoint, never broadcasts,
   and never advances the saved ladder stage:

   ```bash
   launch-guard --preflight-auto-sell-mint TOKEN_MINT
   ```

   Require `"result": "PASSED"` and `"broadcast": false` before considering
   live mode. Review the displayed input amount, expected/minimum USDC output,
   price impact, and simulation units.

7. After reviewing the imported cost bases, dry-run behavior, and passing
   preflight, stop the
   process, change `AUTO_SELL_LIVE=true`, and restart. On macOS, this keeps the
   machine from idle-sleeping while the bot is running:

   ```bash
   caffeinate -i launch-guard --mode portfolio --portfolio-window
   ```

The Mac must remain awake and online. For an immediate per-token kill switch,
run `launch-guard --disarm-auto-sell-mint TOKEN_MINT` in another Terminal; the
running monitor reloads the armed policy on each poll. To stop all execution,
press Control-C. Changing `AUTO_SELL_LIVE=false` takes effect after the process
is restarted.

### Wallet-wide automatic sells

Portfolio-signal execution is separate from the 2x/3x ladder and remains off
by default. When enabled, every priced, non-excluded SPL holding in the selected
wallet is evaluated on each portfolio poll:

- `TAKE PARTIAL` sells 50% once;
- `PROTECT PROFIT` sells 100% once;
- `EXIT WARNING` sells 100% once.

The fractions are configurable. Each mint/decision pair has a unique stored
execution key, so a persistent state cannot submit the same rule repeatedly.
Every live attempt is signed locally and RPC-simulated immediately before the
Jupiter execution call. USDC is excluded by default. Add stablecoins,
long-term holdings, or unwanted tokens to `AUTO_SELL_EXCLUDED_MINTS` before
enabling this mode.

Profit-based states require a verified USD cost basis. A wallet holding with
unknown cost basis can still reach `EXIT WARNING` from severe momentum reversal
or a liquidity break, but it cannot calculate `TAKE PARTIAL` or
`PROTECT PROFIT` from return percentage.

Configure wallet-wide rules in dry-run first:

```dotenv
AUTO_SELL_ENABLED=true
AUTO_SELL_LIVE=false
AUTO_TRADE_FLOOR_PERCENTAGES=false
AUTO_SELL_PORTFOLIO_SIGNALS=true
AUTO_SELL_TAKE_PARTIAL_FRACTION=0.5
AUTO_SELL_PROTECT_PROFIT_FRACTION=1.0
AUTO_SELL_EXIT_WARNING_FRACTION=1.0
AUTO_SELL_MIN_VALUE_USD=1.0
AUTO_SELL_MAX_PRICE_IMPACT_PCT=5.0
AUTO_SELL_MAX_SLIPPAGE_BPS=500
AUTO_SELL_ADAPTIVE_CHUNKS=false
AUTO_SELL_MIN_CHUNK_FRACTION=0.01
AUTO_SELL_MAX_CHUNK_ATTEMPTS=8
AUTO_SELL_EXCLUDED_MINTS=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
```

Preflight a representative 50% wallet-owned sale without broadcasting:

```bash
launch-guard --preflight-owned-auto-sell-mint TOKEN_MINT
```

Require `"result": "PASSED"` and `"broadcast": false`. The per-mint emergency
block works even when no cost basis was imported:

```bash
launch-guard --disarm-auto-sell-mint TOKEN_MINT
launch-guard --allow-owned-auto-sell-mint TOKEN_MINT
```

When `AUTO_SELL_ADAPTIVE_CHUNKS=true`, this preflight starts with the configured
`TAKE PARTIAL` amount and halves only after a price-impact or slippage rejection.
It stops at `AUTO_SELL_MIN_CHUNK_FRACTION` or the attempt limit. The result shows
`configured_fraction`, `selected_fraction`, `adaptive_attempts`, and every
rejected quote. It also separates Jupiter's reported slippage from the slippage
derived from the minimum-output threshold. A passing smaller chunk does not
weaken either configured cap.

For live wallet-wide signals, Launch Guard submits at most one simulated chunk
per token per portfolio polling cycle. The original target and confirmed total
are stored in SQLite, so a restart continues only the unsold remainder. An
uncertain execution freezes the whole batch for review. Profit-ladder principal
recovery is not adaptively chunked because its minimum principal output must be
satisfied by one guarded order.

#### Recover a frozen sell batch

Keep both live flags false and stop every running Launch Guard process before
reviewing a frozen batch. First list the records, then perform a read-only
on-chain balance comparison using the exact quoted `batch_key`:

```bash
launch-guard --auto-sell-review-status
launch-guard --reconcile-auto-sell-review 'BATCH_KEY'
```

Reconciliation does not sign or broadcast a transaction. Continue only when
the result is `BALANCE_UNCHANGED`, `eligible_to_resolve` is `true`, and
`execution_signature` is empty. Also inspect the wallet history independently
in a Solana explorer. If a signature exists, the balance changed, or the result
is inconclusive, leave the record frozen and investigate it on-chain.

After confirming that no transaction occurred, clear the failed attempt while
leaving the overall batch paused:

```bash
launch-guard \
  --resolve-auto-sell-review 'BATCH_KEY' \
  --confirm-no-transaction
```

This records `CLEARED_NO_TRANSACTION`, advances to a new idempotency key, and
does not broadcast. Recheck status. Only after all monitor processes are still
stopped should the paused batch be made eligible for a future monitor run:

```bash
launch-guard \
  --resume-auto-sell-batch 'BATCH_KEY' \
  --confirm-monitor-stopped
```

Resume also does not sign or broadcast. A later live portfolio monitor may
attempt the remaining target under the current quote, simulation, impact, and
slippage guards. For failures recorded before v0.20, the original Jupiter HTTP
response cannot be recovered; full-exit batches can still use the stored target
and unchanged on-chain balance for read-only reconciliation.

### Guarded automated buys

The buyer is independent from Fomo's user interface. It watches Launch Guard's
existing Solana recommendation candidates and considers a purchase only when
all of the following are true:

- the exact mint was manually allow-listed, or automatic discovery is enabled
  and the mint is not excluded;
- the candidate has a confirmed `BUY NOW` or `BUY ZONE` state;
- automatic selections meet the score/liquidity thresholds and are no older
  than the configured signal-age limit;
- fewer than two bot-managed positions are open or pending;
- the wallet does not already hold that mint, preventing mixed cost bases;
- the wallet has enough USDC and Jupiter reports price impact and effective
  slippage within their caps;
- the transaction is locally signed and RPC-simulated before execution.

The first two confirmed purchases use at most 5 USDC each. Those are the only
principal-funded purchases. Afterward, a purchase can use only the accumulated
reinvestment pool, up to 5 USDC and only when at least 1 USDC is available.
For a bot-managed token sale:

```text
allocated cost = original USDC cost × sold tokens / originally bought tokens
realized profit = confirmed USDC received − allocated cost
reinvestment credit = max(realized profit, 0) × 50%
```

Returned principal, unrealized gains, and losses never increase the pool. After
the second profit-ladder stage, the managed position is marked complete so a
new slot can open; the ladder's final token remainder stays in the wallet and
is not counted as an active bot-managed position.

Configure dry-run buying first:

```dotenv
AUTO_BUY_ENABLED=true
AUTO_BUY_LIVE=false
AUTO_TRADE_FLOOR_PERCENTAGES=false
AUTO_BUY_DISCOVERY=true
AUTO_BUY_DISCOVERY_MIN_SCORE=70
AUTO_BUY_DISCOVERY_MIN_LIQUIDITY_USD=50000
AUTO_BUY_SIGNAL_MAX_AGE_SECONDS=30
AUTO_BUY_EXCLUDED_MINTS=
AUTO_BUY_SEED_SIZE_USDC=5.0
AUTO_BUY_MAX_SEED_BUYS=2
AUTO_BUY_MAX_OPEN_POSITIONS=2
AUTO_BUY_REINVEST_PROFIT_PCT=50.0
AUTO_BUY_MAX_PRICE_IMPACT_PCT=5.0
AUTO_BUY_MAX_SLIPPAGE_BPS=500
```

Automatic discovery creates a one-shot policy when an eligible final signal
appears. Manual allow-listing and preflight remain available for a specific
mint:

```bash
launch-guard --arm-auto-buy-mint TOKEN_MINT --buy-symbol SYMBOL
launch-guard --auto-buy-status
launch-guard --preflight-auto-buy-mint TOKEN_MINT
```

Require `"result": "PASSED"` and `"broadcast": false`. Preflight does not
consume a seed buy, debit the profit pool, open a position, or disarm the mint.
Live buying cannot be enabled unless both `AUTO_SELL_ENABLED=true` and
`AUTO_SELL_LIVE=true`, ensuring a confirmed purchase is automatically imported
with its exact USDC cost basis and armed for the 2x/3x sell ladder. After both
buy and sell preflights have been reviewed, live mode is started with:

```bash
caffeinate -i launch-guard --mode all --recommendations-window --portfolio-window
```

Each manual or discovered policy permits one confirmed purchase and is
automatically disarmed afterward. A previously disarmed policy is not
automatically rearmed. Use `--disarm-auto-buy-mint TOKEN_MINT` as the per-mint
kill switch and set `AUTO_BUY_LIVE=false` before restarting to disable all
purchases.

The current [Fomo Terms](https://fomo.family/terms) prohibit automated scripts
or bots from executing trades or controlling account activity on Fomo's
services. Launch Guard does not automate the Fomo app, call a Fomo endpoint, or
reuse a Fomo login session; it submits direct on-chain Jupiter transactions
from the exported self-custodied wallet. The Terms do not expressly confirm
whether Fomo considers that separate activity permissible, so obtain written
clarification from Fomo if continued account compatibility matters.

### Entry-price and pullback decisions

Without `AUTO_BUY_ENABLED`, the board is a read-only decision-support system:

```text
DISCOVER → SCORE → WAIT → CONFIRM → BUY ZONE → ALERT → YOU DECIDE
```

Each qualified token receives one of seven states:

- `ENTRY PENDING`: entry conditions are present but have not yet survived the
  configured number of consecutive checks;
- `BUY NOW`: an unextended setup passed all entry gates for three consecutive
  checks by default;
- `WAIT FOR PULLBACK`: price or five-minute momentum crossed the configured
  extension threshold, so Launch Guard anchors a preferred entry zone 4–6%
  below that price;
- `PULLBACK STARTED`: price has fallen at least the configured amount from its
  tracked peak but remains above the anchored entry zone;
- `BUY ZONE`: price entered the anchored zone and passed the score, liquidity,
  volume, momentum, and buyer-flow gates for the required consecutive checks;
- `WATCH`: confirmation is incomplete or price fell through the entry zone;
- `AVOID`: liquidity fell below the safety floor, liquidity collapsed by more
  than 35%, or falling momentum coincides with heavy sell pressure.

The pullback zone remains fixed after it is created. It does not recalculate
from every lower quote. The separate Terminal displays current price, preferred
entry range, remaining pullback, momentum, liquidity, volume direction, risk,
and the reason for the current state. Transitions into `PULLBACK STARTED` and
`BUY ZONE` produce one-time alerts for those transitions.

The entry process is inspired by general risk-discipline and probabilistic
thinking principles commonly discussed in trading literature. It is not a
replica of, or strategy endorsed by, any particular author. Requiring repeated
evidence reduces one-poll signal flips but cannot prevent a market from
reversing after a confirmed entry.

When an entry becomes confirmed, the board anchors a reference entry, stop,
and first objective. The objective is never below the configured minimum
reward-to-risk multiple. These levels are a consistency framework rather than
a forecast; Launch Guard does not place the stop or target on any exchange.

These states are deterministic heuristics based on incomplete market data—not
predictions or instructions to trade. They trigger an order only when the mint
has a one-shot manual/discovered policy and both auto-buy switches are enabled.
The optional seller uses the exact 2x/3x ladder and separately enabled
portfolio-signal rules. Neither path requests Fomo login or Robinhood
credentials.

### Optional phone alerts with Pushover

Phone notifications use Pushover's HTTPS Message API and remain separate from
every exchange or wallet. Create a Pushover account, install its phone app,
register a Launch Guard application, and copy the application token and your
user key into your local `.env`:

```dotenv
PUSHOVER_ENABLED=true
PUSHOVER_APP_TOKEN=YOUR_30_CHARACTER_APP_TOKEN
PUSHOVER_USER_KEY=YOUR_30_CHARACTER_USER_KEY
PUSHOVER_DEVICE=
PUSHOVER_ALERT_DECISIONS=BUY NOW,BUY ZONE,PULLBACK STARTED,WAIT FOR PULLBACK,AVOID
PUSHOVER_MIN_SCORE=60
PUSHOVER_COOLDOWN_SECONDS=300
PUSHOVER_PORTFOLIO_ALERT_DECISIONS=TAKE PARTIAL,PROTECT PROFIT,EXIT WARNING
PUSHOVER_HIGH_PRIORITY_DECISIONS=BUY NOW,BUY ZONE,TAKE PARTIAL,PROTECT PROFIT,EXIT WARNING
PUSHOVER_PORTFOLIO_COOLDOWN_SECONDS=300
```

Never commit the token or user key to GitHub. They are notification credentials,
not trading credentials, but they should still be kept private.

Verify the connection before starting the scanner:

```bash
launch-guard --test-notification
launch-guard --test-high-priority-notification
```

When monitoring is active, Launch Guard sends only configured decision states
that meet the minimum score. It stores successful sends in SQLite, so restarting
the program does not repeat the same state for the same token. A later state
change can notify after the cooldown. Time-sensitive `PULLBACK STARTED` and
`BUY ZONE` transitions and safety `AVOID` transitions bypass the cooldown but
are still deduplicated. `AVOID`
also bypasses the minimum-score filter so an invalidation is not hidden after
the score falls. Each message includes the chain, score,
price, anchored entry zone when available, momentum, liquidity, volume trend,
risk, decision reason, and a DEX Screener link.

Pushover priority `1` is used for the states in
`PUSHOVER_HIGH_PRIORITY_DECISIONS`. Pushover documents that priority `1`
bypasses quiet hours, plays a sound, and highlights the message in red. Launch
Guard does not use emergency priority `2`, so alerts do not repeat until
acknowledged. Holding alerts are emitted only when a token changes into a
configured sell/profit-protection state and are rate-limited per token.

`WATCH` is intentionally excluded from the default phone list to reduce noise.
Add it to `PUSHOVER_ALERT_DECISIONS` if you want those notifications too.

### Fomo multichain recommendations and wallet monitoring

Run the Robinhood Chain scanner with the separate recommendation window:

```bash
launch-guard --mode robinhood --recommendations-window
```

Run Solana launches, Solana wallet copy monitoring, all seven EVM discovery
feeds, EVM wallet activity, and HyperCore monitoring together:

```bash
launch-guard --mode all --recommendations-window
```

Use multichain mode without the Solana launch feed:

```bash
launch-guard --mode multichain --recommendations-window
```

The scanner reads DEX Screener's latest and recently updated token profiles for
Ethereum, Base, BNB Smart Chain, BOB, Monad, Robinhood Chain, and HyperEVM. It then scores each
token's most liquid pair on that same chain. A qualifying row uses a USD price,
shows the exact EVM contract, and includes a DEX Screener market URL. Add exact
contracts to `.env` when you want them monitored even if they are not present
in the current profile feed:

```dotenv
BASE_TOKEN_ADDRESSES=0xContractOne,0xContractTwo
BNB_TOKEN_ADDRESSES=0xContractThree
MONAD_TOKEN_ADDRESSES=0xContractFour
```

To monitor the same Fomo public address on every configured EVM network and on
HyperCore:

```dotenv
EVM_WALLET_ADDRESS=0xYourPublicAddress
HYPERLIQUID_ADDRESS=0xYourPublicAddress
```

ERC-20 `Transfer` logs prove that tokens moved into or out of the address, but
they do not by themselves prove that the movement was a buy or sell. Launch
Guard therefore prints EVM activity as `IN` or `OUT`. HyperCore fills come from
the exchange's public info API and are printed as `BUY` or `SELL`.

Ethereum, Base, BNB, BOB, and Monad RPC values are left blank in `.env.example`.
Add read-only provider endpoints that you trust before their wallet watchers
will start. Robinhood Chain and HyperEVM use the public endpoints documented by
those networks.

BNB Chain's documented public endpoints disable `eth_getLogs`, which this
wallet watcher needs. Use a trusted third-party BNB RPC that supports
`eth_getLogs`; discovery and recommendation scanning work without a BNB RPC.

Official Robinhood Stock Token contracts are removed using Robinhood's live
asset registry. Launch Guard also conservatively excludes matching direct and
wrapped stock symbols, such as `NVDA` and `wNVDAx`, on the other discovery
feeds. Tokenized securities are not the memecoin/crypto-token feed this mode is
designed for. Symbol filtering is a safeguard, not an exhaustive legal
classification system.

DEX Screener discovery does not prove that Fomo currently exposes or permits a
trade for every contract. Verify the chain, contract, quote, slippage, and fees
inside Fomo before taking any manual action. The bot does not log in to Fomo;
automated buying is limited to guarded manual or automatically selected Solana
mints and never applies to the EVM discovery feeds.

Review stored EVM and HyperCore activity with:

```bash
launch-guard --wallet-info 0xYourPublicAddress
```


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

Then monitor it with the holdings board:

```bash
launch-guard --mode portfolio --portfolio-window
```

Importing writes a local cost-basis record only. It does not connect to Fomo,
submit an order, arm the mint, or move the real holding. Never put a Fomo
password, session cookie, recovery phrase, or private key in the project,
`.env`, logs, or GitHub. The optional signing key belongs only in the hidden
keychain prompt described above.
