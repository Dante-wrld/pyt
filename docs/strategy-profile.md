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

Results come from what actually moved in CopyFomo's wallet (USDC, or SOL), so they
include every fee and all slippage. Tokens are identified by the mint in the
on-chain trade, never by name. Legs that cannot be priced from USDC or SOL
flow (token-to-token swaps, airdrops, positions paid in both currencies,
sells of positions opened before monitoring began) are listed as UNPRICED
and left out of the totals rather than guessed.

To see results per leader, list the wallets CopyFomo copies in
`COPYFOMO_LEADER_WALLETS` (`name:ADDRESS,...`). They are watched read-only,
like CopyFomo's own wallet. A CopyFomo position is attributed to the leader
whose buy of the same mint came within 10 minutes before it; when two
leaders bought the same mint in that window it is marked ambiguous instead
of guessed. A leader buy CopyFomo did not copy within the window is counted
as skipped, and the report compares how the leaders' own copied and skipped
trades did. `launch-guard-eval track` also follows CopyFomo's and the
leaders' buys as entries (`wallet:COPYFOMO`, `wallet:leader:<name>`), so
`report --exit-model ladder --group wallet:` shows how your exits would have
done on the same trades.

CopyFomo trades in USDC. Each leg is priced from the wallet's USDC change in
the same transaction, falling back to SOL, so results come out in USDC.

## Leader-held feed

`FEED_LEADER_HOLDINGS=true` reads what the wallets in `COPYFOMO_LEADER_WALLETS`
currently hold every `LEADER_HOLDINGS_POLL_SECONDS` and puts those tokens on
the board, so the same entry rules as every other feed decide whether and
when to buy. Nothing waits for a leader to trade.

Holding is a discovery source, not a signal, so it is filtered: holdings
under `LEADER_HOLDINGS_MIN_USD`, pools under `LEADER_HOLDINGS_MIN_LIQUIDITY_USD`
and tokenized stocks are skipped, and at most `LEADER_HOLDINGS_MAX_CANDIDATES`
tokens (largest combined leader value first) are added per poll so they
cannot crowd the 30-slot board. They can still displace lower-scoring board
candidates; watch the board if momentum candidates start disappearing.

Board candidates now record which feeds found them (`sources`). A token that
only `leader-held` found is never bought live while that source is listed in
`ENTRY_SHADOW_ONLY_SOURCES` (the default); it is still signalled, tagged and
tracked. A token another feed also found is unaffected. `report --group
signal:` splits each signal type by source, so leader-held signals can be
compared with the momentum feed's before the source is released.

When a leader sells at least 25% of a token that is on the board or that the
bot holds, a `LEADER_SELL` event is recorded and logged as a warning. It is
record-only, for testing as an exit rule later.

coin_tracker's dynamic watchlist reads the same board, so leader-held tokens
that pass its age and liquidity floors will appear there too.

