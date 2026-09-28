"""Closed-candle research for shadow entries and existing-position reviews.

Fixed experimental parameters, not a validated trading edge. No wallet code.
"""

from __future__ import annotations

import math

from .market_structure import Candle

PERIOD = 900
MIN_BARS = 150
MIN_AGE_SECONDS = 3 * 86400


def ema(values: list[float], period: int) -> list[float]:
    out = [values[0]]
    alpha = 2 / (period + 1)
    for value in values[1:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def assess_trend_pullback(
    candles: list[Candle],
    *,
    now: float,
    pair_created_at_ms: float | None,
) -> dict:
    """15m EMA20 > EMA50, rising EMA20, then a closed pullback/reclaim.

    Pool age is an available proxy, not a claim about token creation time.
    Missing, future, stale, malformed or gapped data never confirms a signal.
    """
    result = {
        "valid": False,
        "trend_confirmed": False,
        "pullback_confirmed": False,
        "period_seconds": PERIOD,
        "reason": "insufficient closed-candle history or pool age",
    }
    if (
        not math.isfinite(now)
        or pair_created_at_ms is None
        or not math.isfinite(pair_created_at_ms)
        or now - pair_created_at_ms / 1000 < MIN_AGE_SECONDS
        or len(candles) < MIN_BARS
    ):
        return result
    bars = candles[-MIN_BARS:]
    if not 0 <= now - (bars[-1].start + PERIOD) <= PERIOD:
        return {**result, "reason": "latest candle is open, future or stale"}
    if any(b.start - a.start != PERIOD for a, b in zip(bars, bars[1:], strict=False)):
        return {**result, "reason": "gapped or duplicate candles"}
    for bar in bars:
        if (
            not all(
                math.isfinite(v)
                for v in (bar.open, bar.high, bar.low, bar.close, bar.volume)
            )
            or bar.low <= 0
            or bar.volume < 0
            or bar.low > min(bar.open, bar.close)
            or bar.high < max(bar.open, bar.close)
        ):
            return {**result, "reason": "invalid OHLCV"}
    fast = ema([b.close for b in bars], 20)
    slow = ema([b.close for b in bars], 50)
    latest = bars[-1]
    active = all(b.volume > 0 for b in bars[-4:])
    trend = (
        active and latest.close > fast[-1] > slow[-1] and fast[-1] > fast[-2] > fast[-3]
    )
    # A prior closed bar touched EMA20 but did not collapse below EMA50.
    touch = any(
        b.low <= fast[i] * 1.005 and b.high >= fast[i] * 0.995 and b.close >= slow[i]
        for i, b in enumerate(bars[-4:-1], len(bars) - 4)
    )
    reclaim = latest.close > latest.open and latest.close > bars[-2].high
    not_extended = latest.close <= fast[-1] * 1.03
    pullback = bool(trend and touch and reclaim and not_extended)
    return {
        **result,
        "valid": True,
        "trend_confirmed": bool(trend),
        "pullback_confirmed": pullback,
        "as_of": latest.start + PERIOD,
        "ema20": fast[-1],
        "ema50": slow[-1],
        "close": latest.close,
        "reason": (
            "trend and pullback reclaim confirmed"
            if pullback
            else "uptrend without a confirmed pullback entry"
            if trend
            else "uptrend not confirmed"
        ),
    }


def fresh_evidence(evidence: dict, *, now: float) -> bool:
    try:
        return (
            bool(evidence.get("valid"))
            and 0 <= now - float(evidence["as_of"]) <= PERIOD
        )
    except (KeyError, TypeError, ValueError):
        return False


def entry_confirmed(evidence: dict, price: float, *, now: float) -> bool:
    """The current quote must still be near support, not chase an old signal."""
    try:
        fast = float(evidence["ema20"])
        return (
            fresh_evidence(evidence, now=now)
            and bool(evidence.get("pullback_confirmed"))
            and math.isfinite(price)
            and math.isfinite(fast)
            and 0 < fast < price <= fast * 1.03
        )
    except (KeyError, TypeError, ValueError):
        return False
