# Portfolio structure watch (advisory)

This signal refines an existing `REBOUND WATCH`. It never requests or submits a buy.
Exit warnings and profit protection are evaluated before candle analysis.

## Data and timeframe pairs

Read pool OHLCV in USD from GeckoTerminal's public API. Reject missing pool
metadata, a token-address mismatch, invalid OHLCV, unclosed bars, gaps in the
nine higher or eight lower candles used, and stale bars. Require the last
lower-frame close to be within 15% of the current DEX Screener USD quote.
Cache responses for 5 minutes and cap attempts at 8 per rolling minute.
If unavailable, leave the original `REBOUND WATCH` unchanged.

| Pool age | Key level | Entry frame |
| --- | --- | --- |
| 14 days or more | D | H1 |
| 3 to 13 days | H4 | M15 |
| 12 hours to under 3 days | H1 | M5 |
| 3 to under 12 hours | M15 | M1 |
| Under 3 hours | No structure watch | No structure watch |

## Explicit heuristic

- Use eight consecutive completed higher-frame bars to define a range.
- The ninth bar must dip more than 0.5% below the range low and close back
  above that low. This is a mechanical *sweep* definition.
- On eight consecutive completed lower-frame bars, define the break level
  from the first three highs. Require a later close above it, then a touch
  of that level within 1% and a final close above it.
- Place the displayed invalidation reference at the sweep low. Display the
  old range high as a possible reference target only if the distance to that
  level is at least 1.5 times the distance to invalidation.
- The alert shows these observed levels. They are **not** orders or a
  calibrated probability of another high.

This uses a range sweep and retest related to Wyckoff-style range analysis.
It does **not** claim to detect a fair-value gap or reproduce either video
exactly. Thresholds are implementation choices and have not been backtested.
See [Wyckoff analysis](https://chartschool.stockcharts.com/table-of-contents/market-analysis/wyckoff-analysis-articles/wyckoff-market-analysis)
and [GeckoTerminal OHLCV FAQ](https://apiguide.geckoterminal.com/faq).
