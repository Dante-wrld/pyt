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

## Shadow trend entries and portfolio-approved swing holds

Two swing designs run side by side on paper, because neither is proven and
they take different risks:

- **Managed swing hold (this section).** A management mandate on an existing
  hunter position, not a buyer. Hunter opens the paper position. The swing
  module supplies evidence; the portfolio exit path approves or rejects the
  hold and executes the normal profit stages and risk exits. It keeps the
  normal stop and never adds. Off unless `SWING_SHADOW_MANAGER_ENABLED=true`.
- **Standalone swing-v1 (`launch-guard-swing`).** Its own $30 paper book:
  buys hunter-v1's BUY_READY entries itself, holds up to 24h with a 35% stop
  from the average cost, a wider trailing stop, no stagnation exit, and
  averages down at most twice (half the first stake at -20% vs average
  cost; `SWING_MAX_ADDS=0` turns that off). Up to $10 per position, so it
  loses more on rugs; the question it answers is whether riding out dips
  pays on these tokens. `launch-guard-swing --status` shows its results.

A swing mandate reserves the position's **remaining cost basis** against a
$30 management limit. This is an internal responsibility allocation, not a
new funded account: it does not create cash, return money to hunter, transfer
tokens, or book profit. Source ownership and P&L stay in the original account.
Partial exits release the corresponding capacity; closing releases the rest.
The reservation persists with the position, and status exposes reserved and
available capacity. Do not count this limit as another $30 of portfolio equity.

The experimental hold rule requires fresh 15-minute EMA trend evidence, price
at least 97% of entry, liquidity at least 90% of entry and above the policy
floor, and known positive buying flow with buys >= sells. Reviews can assign
management before stagnation. Approval only defers the ordinary stagnation
exit. It never widens the original stop, overrides a reversal/emergency exit,
or changes principal recovery and profit stages. A confirmed trend failure
invalidates the hold. Evidence expires after 15 minutes; missing/stale evidence
restores ordinary exits. The maximum hold is 24 hours from the **original
entry**, not from delegation or a process restart.

### Run the controlled paper comparison

After updating/installing the code (`.venv/bin/pip install -e .`), keep the
normal recommendation monitor running and start:

```bash
.venv/bin/launch-guard-trend-shadow
```

For a single cycle or a report:

```bash
.venv/bin/launch-guard-trend-shadow --once
.venv/bin/launch-guard-trend-shadow --status
```

No wallet, signer, OpenAI model, or live trader is invoked. It uses a separate
`launch_guard_trend_shadow/` directory and three virtual $30 hunter books:

| Arm | Entry | Exit |
| --- | --- | --- |
| baseline | Deterministic eligible candidate | Ordinary hunter rules |
| trend | Same opportunity, only if trend + pullback qualifies | Ordinary rules |
| managed | Exact same entry as baseline | Portfolio-approved swing hold |

The baseline and managed arms buy the same mint, price and size in the same
cycle. The filtered arm accepts or skips that same opportunity. All arms must
have capacity before the next entry, and no mint is re-entered while any arm
still holds it. This deliberately controls entry selection to compare rules;
it does **not** estimate independent-strategy throughput. Existing live-entry
profile, liquidity and confirmation gates apply, with $5 entries, two open
positions, $3 realized daily-loss limit, and shared re-entry checks. These are
paper fills without the live model, transaction preflight or execution route.

The trend filter uses a pool age of at least three days and 150 contiguous,
closed 15-minute candles (37.5 hours of history). Pool age is not token mint
creation time. EMA20 must exceed EMA50 and rise over the latest three samples,
with price above EMA20. One of the preceding three bars must touch EMA20
(within 0.5%) and close above EMA50. The latest bar must be bullish and close
above the previous high, no more than 3% above EMA20. The current entry quote
must still be above EMA20 and within that 3% band. Recent volume must be
positive. Open, stale, gapped or malformed candles never qualify. These fixed
parameters are hypotheses, not proven settings. This experiment uses EMA
confirmation; it does not implement Supertrend or breaker blocks.

The runner fetches at most eight candle requests per minute, prioritizing held
positions. It writes `launch_guard_trend_evidence.json`, per-cycle decisions,
and a report with realized/unrealized P&L, completed trades and drawdown.
The default round-trip cost estimate is 1.2%, charged proportionally on exits
in every arm, with no separate fixed network fee. It is an assumption, not a
measured fill. Set `--round-trip-cost-pct` to test another assumption in a **new
--directory**; costs cannot silently change in an existing experiment.
No results are evidence of improved returns until sufficient forward samples
have completed, including losing and open positions.

### Opt in for the existing hunter shadow loop

The comparison itself always tests the manager in its managed arm. To also
apply it to the existing hunter shadow book, run the evidence producer above
and enable:

```dotenv
SWING_SHADOW_MANAGER_ENABLED=true
SHADOW_TREND_EVIDENCE_PATH=launch_guard_trend_evidence.json
```

Then restart only the hunter shadow loop. This flag is read by the shadow
path only; live trading behavior is unchanged. The ordinary shadow loop stays
the sole writer/executor of its capital book. The research process only reads
it for held mints and writes candle evidence. Use absolute paths when running
from different directories. Do not run multiple writers against the same book.

### Historical swing replay

`launch-guard-eval report --exit-model swing` replays the standalone swing-v1
exits and adds, not the candle-based manager.
Its simulated adds now require retention of at least 70% of entry liquidity,
and stop, trailing and maximum-hold exits are evaluated before adding. Flow
and portfolio cash constraints are still approximations in the price replay.
Compare with `SWING_MAX_ADDS=0` when studying that historical scenario.

The outcome tracker samples every 15 minutes from hour 1 to hour 24. Sparse
samples can miss intervening price moves; data from before that sampling
change is especially limited for multi-hour evaluation.

## Re-entry after losses (live and paper)

After two losing sells in a row a mint is blocked until its price reclaims
the entry price of the first trade in that losing streak; then it
"graduates" and may be bought again, subject to every other entry rule.
hunter-v1 live, hunter-v1 paper and swing-v1 all use the same rule
(`reentry_block_reason`).
