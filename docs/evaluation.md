# Evaluating strategies with recorded outcomes

The bot stores launches and decisions, but not what prices did afterwards, so
there is no way to tell whether a change helped or you got lucky.
`launch-guard-eval` fills that gap in two steps.

## 1. Record outcomes (run beside the bot)

```bash
launch-guard-eval track
```

- Opens `launch_guard.db` and the live-trial ledger **read-only** and writes
  only to `launch_guard_outcomes.db`. It holds no keys and sends no orders.
- Sources: launch risk decisions, board candidates accepted by the
  intelligence layer (`intelligence_scores`, which is where Solana momentum
  candidates appear), and live-trial agent decisions.
- For each new decision it samples the price immediately (the realistic
  entry), every 60s for the first hour for accepted/agent-considered tokens,
  and at 5m, 15m, 1h, 4h and 24h for everything.
- Rejected launches are sampled at `--reject-sample-rate` (default 10%,
  deterministic by mint), which keeps the DEX Screener request budget bounded
  while giving each rejection reason an unbiased sample.
- It only tracks decisions made within `--max-decision-lag` (300s) of it
  seeing them, so it has to be running while the bot runs.

## 2. Report (offline)

```bash
launch-guard-eval report
launch-guard-eval report --take-profit-pct 50 --stop-loss-pct 25 --max-hold-seconds 1800
launch-guard-eval report --slippage-bps 500 --json
```

For every group (`launch:ACCEPTED`, `launch:REJECTED`, `scored:<tier>`,
`ledger:<agent>:<state>`)
it prints net results after costs on all data, the earlier 70% (train) and
the later 30% (test), plus how tokens looked at each horizon. It then shows,
for each rejection reason, what those tokens would have made if bought under
the same rules.

Honest-by-default conventions:

- Entry is the first observed quote, not the launch price.
- Take-profit fills at the lower of target and observed price; stop-loss fills
  at the observed price even when it gapped far through the stop.
- A token whose quote disappears or whose liquidity drops below
  `--min-exit-liquidity-usd` and never recovers counts as a total loss.
- Verdicts require 100+ test trades and a 95% bootstrap interval that excludes
  zero. Filter verdicts require non-overlapping intervals.

## How to use it without fooling yourself

1. Explore rule changes against **train** only.
2. Decide what counts as success before looking at **test**.
3. Look at test once per idea. If you keep tweaking until test looks good,
   test has become train.
4. Only change live settings after test agrees.

## Comparing buy signals (MOMENTUM BUY vs BUY ZONE)

The bot records every move into a buy decision in `buy_signals`: time, mint,
price, reason, and whether the live entry gate would have blocked it. It logs
the change, not every poll the candidate stays there. The tracker gives each
signal its own price samples starting at the moment it fired, even when the
token is already being tracked, so a MOMENTUM BUY that fires 40 minutes after
a token appeared is measured from that moment.

```bash
launch-guard-eval report --group signal:
launch-guard-eval report --group "signal:MOMENTUM BUY" --take-profit-pct 20 --stop-loss-pct 10
```

When both groups have data, the report prints them side by side on the test
set and applies the promotion rule agreed before any results: MOMENTUM BUY
goes live only with 100+ test signals, a 95% interval above zero, and an
average no worse than BUY ZONE's. Until then it stays shadow-only.

## The live-style exit ladder and overnight sweeps

`--exit-model ladder` replays each entry through a price-only model of the
live exit: principal recovery at `AUTO_SELL_PRINCIPAL_MULTIPLE`, the second
stage at `AUTO_SELL_HALF_PROFIT_MULTIPLE`, a trailing stop that widens to
`PRINCIPAL_RECOVERED_TRAILING_STOP_PCT` once the principal is back, the
stagnation exit, and a max hold. Settings default to your `.env`, so the
simulation matches what the bot runs. Each sell leg pays its own slippage,
fees and network fee.

What it cannot model: the live reversal exits also use momentum and buy/sell
counts, which the tracker does not record, and the live trailing stop waits
for selling pressure where the simulated one fires on price alone. The hard
stop is modelled exactly: the live bot applies `STOP_LOSS_PCT` on price
alone while principal is outstanding, the same as `--ladder-stop-loss-pct`.

```bash
launch-guard-eval report --exit-model ladder --group signal:
launch-guard-eval sweep --group "signal:MOMENTUM BUY"
launch-guard-eval sweep --group "signal:BUY ZONE" --stops 15,20,25 --trails 10,12,15
```

`sweep` tries every combination of stop, trailing stop, trailing activation,
principal multiple and stagnation window, ranks them on the train part only,
and shows how the top ones did on test next to your current live settings.
The top train row nearly always looks better than it will perform; a setting
earns trust only if its test result holds up. Change live settings once, after
the freeze, not after every sweep.

## Candle patterns at signal time

Each buy signal is tagged in the background with the shape of the latest
closed 1-minute candle (GeckoTerminal OHLCV), using the classic definitions:
marubozu, hammer / hanging man, inverted hammer / shooting star, doji
variants, spinning top. Hammer vs hanging man and inverted hammer vs shooting
star are the same shapes; the prior trend decides which. A candle that could
not be read is tagged `unavailable`, never skipped, so it cannot bias the
comparison. Tagging uses its own request budget and never delays the monitor.

`report` then splits each signal type by pattern, e.g. MOMENTUM BUY on a
bullish marubozu vs on a shooting star, with the same costs and exits.
Signals from before tagging existed show as `untagged`. Compare patterns
within one signal type only, and treat a pattern as worth a rule only when
its interval clears the others'.

## Restricting to a time window

`report` and `sweep` take `--since` to use only decisions from a given local
time on: `HH:MM` (the most recent such time), `YYYY-MM-DD[ HH:MM]`, or epoch
seconds. Use it to judge exits only on signals recorded after the tracker's
dense window was lengthened, e.g.:

```bash
launch-guard-eval report --exit-model ladder --group "signal:MOMENTUM BUY" \
    --since "2026-09-25 12:07" --stagnation-window 0
```

Prefer the full date over a bare `HH:MM` once a day has passed, so the window
does not silently move.

## Candidate exit rules

By default the live ladder protects nothing between break-even and the
trailing-stop activation (+20%), so a signal that peaks at +12% can still end
at the stop. Two candidate rules exist, off by default:

- `--lock-after-gain-pct X --lock-stop-pct Y`: once up X%, the stop rises to
  Y% above entry. With the default costs a Y of about 8 locks in a small win.
  The live bot runs the same rule from `LOCK_AFTER_GAIN_PCT` /
  `LOCK_STOP_PCT`, and these flags default to those values, so once a sweep
  result holds up on test you switch it on in `.env` with no code change.
- `--early-take-pct X --early-take-fraction F`: sell F of the position once,
  at +X% (simulation only).

## Settings to measure before touching

Two live defaults are guesses that decide most outcomes and have never been
tested against recorded data. Sweep them before changing either:

- `STAGNATION_WINDOW_SECONDS=300` sells anything not up 3% five minutes
  after entry when the 5-minute change is flat or down. On a token that is
  three days old and was bought on a 4-6% pullback, five minutes is noise,
  and each such exit pays the full round trip. `sweep
  --stagnation-windows 0,300,900,1800` answers whether it earns its keep.
- `AUTO_SELL_PRINCIPAL_MULTIPLE=2.0` waits for a double before recovering
  principal. Most winners in this universe top out well short of that, so
  the ladder rarely engages. `sweep --principal-multiples 1.3,1.5,2` shows
  what an earlier first take would have done.

```bash
launch-guard-eval report --exit-model ladder --group signal: --since "2026-09-25 12:07" \
    --lock-after-gain-pct 10
launch-guard-eval sweep --group "signal:EARLY BUY" --since "2026-09-25 12:07" \
    --stops 15,20 --trails 12 --activations 20 --principal-multiples 2 \
    --stagnation-windows 0,300 --locks 0,8,10,15 --early-takes 0,10,15
```

Keep the grid small: the more combinations a sweep tries, the better its best
row looks by luck. A rule earns a place in the live bot only if it holds up
on test data for more than one signal type.

## Measuring real costs

The cost model's defaults (300 bps slippage per side, a 7.4% break-even) are
deliberately harsh guesses. `launch-guard-eval costs` measures instead:

- For the most recently signalled tokens (or `--mints`), a Jupiter quote for
  buying `--position-usd` of the token and a quote for selling exactly those
  tokens back. The gap is the full round-trip cost at that size, pool fees
  and price impact included. Quotes only: nothing is signed or sent.
- For the bot's own confirmed buys, the tokens received versus the quote.

It prints a suggested `--slippage-bps` (half the median round trip plus the
median realized slippage) to pass to `report` and `sweep` with `--fee-bps 0`.
It needs `JUPITER_API_KEY` for the quotes and pauses about a second between
tokens. Re-measure now and then; costs move with liquidity and token mix.

## Multi-hour holds: the swing strategy (swing-v1)

hunter-v1's exits are built for minutes: the 5-minute stagnation exit closes
most entries long before a token's real move, which on 2026-09-27 came a
median ~22 hours later. swing-v1 keeps hunter-v1's entries (same entry
review, live gates and re-entry rule, no model call) and swaps the exits for
multi-hour ones: no stagnation exit, a 35% hard stop, a trailing stop that
arms at +50% and trails 25% (40% once the stake is back), a deeper reversal
bar, and a 24-hour max hold. It is paper only.

The tracker samples every 15 minutes from hour 1 to hour 24
(`--swing-interval`, `--swing-window`), so multi-hour exits can be replayed.
Decisions tracked before that only have 1h/4h/24h points after the first
hour; judge the swing model on data recorded after this change.

    launch-guard-eval report --exit-model ladder --slippage-bps 60 --fee-bps 0
    launch-guard-eval report --exit-model swing  --slippage-bps 60 --fee-bps 0

Same decisions, two exit models: compare them per signal type on the test
split. The swing numbers are a starting point (SWING_STOP_LOSS_PCT,
SWING_TRAILING_ACTIVATION_PCT, SWING_TRAILING_STOP_PCT,
SWING_PRINCIPAL_TRAILING_STOP_PCT, SWING_MAX_HOLD_HOURS, ...), not a result.

Paper trading it live, beside the bot (its own book, launch_guard_swing_capital.json):

    launch-guard-swing            # loop, every 60s
    launch-guard-swing --status   # results and open positions

Positions that fall off the board are priced directly from DEX Screener;
those quotes have no buy/sell flow, so the trailing stop then fires on price
alone, as in the simulator.

## Re-entry after losses (live and paper)

After two losing sells in a row a mint is blocked until its price reclaims
the entry price of the first trade in that losing streak; then it
"graduates" and may be bought again, subject to every other entry rule.
hunter-v1 live, hunter-v1 paper and swing-v1 all use the same rule
(`reentry_block_reason`).
