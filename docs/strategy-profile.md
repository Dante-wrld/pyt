# Strategy profile

`StrategyProfile` (`solana_launch_guard/strategy_profile.py`) makes two things
explicit that used to depend on which credentials happened to be set:

1. **Which feeds run**: `FEED_PUMPPORTAL_LAUNCHES`, `FEED_SOLANA_MOMENTUM`,
   `FEED_LAUNCHLAB`, `FEED_COPYFOMO_WALLETS`, `FEED_MULTICHAIN`.
2. **Which signals may spend money**: `ENTRY_ALLOWED_DECISIONS` (any of
   `BUY NOW`, `BUY ZONE`, `MOMENTUM BUY`, `EARLY BUY`) and
   `ENTRY_MIN_TOKEN_AGE_DAYS` (pool age from the quote; unknown age is blocked
   when this is set).

Both live buy paths check the same gate: the deterministic auto-buyer
(`_maybe_auto_buy`) and the agent live trial (`decide_hunter_entry`). A blocked
signal is logged once per mint per reason as `AUTO-BUY SHADOW` or a
`BUY_ZONE_SKIPPED` ledger row, and it still appears on the board, in alerts
and in the outcome tracker. At startup the bot logs one `STRATEGY PROFILE`
line summarising what is on.

Unset variables keep the previous behaviour.

## Recommended starting profile

```ini
# See the note below on why the launch feed is off.
FEED_PUMPPORTAL_LAUNCHES=false
# The live candidate source.
FEED_SOLANA_MOMENTUM=true
FEED_LAUNCHLAB=false
# Read-only: records CopyFomo's own trades, never buys.
FEED_COPYFOMO_WALLETS=true
COPYFOMO_SOLANA_WALLET=<CopyFomo's wallet address>
FEED_MULTICHAIN=false
ENTRY_ALLOWED_DECISIONS=BUY ZONE
ENTRY_MIN_TOKEN_AGE_DAYS=3
SOLANA_MOMENTUM_MIN_AGE_DAYS=3
AUTO_BUY_DISCOVERY_MIN_LIQUIDITY_USD=50000
AUTO_REBUY_ENABLED=false
# Real copy-trading stays off.
WATCHED_WALLETS=
# LLM agents stay paper/shadow.
AGENT_LIVE_KILL_SWITCH=true
```

Why the launch feed is off: the recommendation pool (30 slots) evicts the
lowest signal score, and fast-pumping new launches often outscore established
tokens, so leaving it on can crowd out the very candidates this profile buys.
Turn it back on later only if you want launch shadow data and the board shows
established tokens surviving alongside it.

Roll-out: run a day with `AUTO_BUY_LIVE=false`, check the `AUTO-BUY SHADOW`
and `STRATEGY PROFILE` lines look right, then decide whether to go live. Keep
`launch-guard-eval track` running throughout and judge the result with
`launch-guard-eval report` against the stopping rule you set in advance.

## CopyFomo evaluation

`FEED_COPYFOMO_WALLETS` only watches CopyFomo's own wallet
(`COPYFOMO_SOLANA_WALLET`) and records its trades; it never scores or buys.
This is different from `WATCHED_WALLETS`, which makes Launch Guard buy what
other wallets buy. Keep that off.

```bash
launch-guard-eval copyfomo          # realized SOL P&L per position and per week
launch-guard-eval copyfomo --json
```

Results come from the SOL that actually moved in CopyFomo's wallet, so they
include every fee and all slippage. Tokens are identified by the mint in the
on-chain trade, never by name. Legs that cannot be priced from SOL flow
(token-to-token swaps, WSOL/USDC routes, airdrops, sells of positions opened
before monitoring began) are listed as UNPRICED and left out of the totals
rather than guessed. The wallet shows what CopyFomo traded, not which leader
it copied; per-leader results need CopyFomo's own history.

