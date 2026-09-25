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
