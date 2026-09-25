from __future__ import annotations

import asyncio
import json
import math
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import certifi

from .geckoterminal import COINGECKO_PRO_ONCHAIN_BASE_URL


@dataclass(frozen=True)
class Candle:
    start: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class StructureEvidence:
    pair: str
    box_low: float
    box_high: float
    sweep_low: float
    break_level: float
    entry: float
    invalidation: float
    target: float

    def description(self) -> str:
        return (
            f"{self.pair} closed-candle range sweep and break/retest; "
            f"box {self.box_low:.8g}-{self.box_high:.8g}; "
            f"entry reference {self.entry:.8g}, invalidation "
            f"{self.invalidation:.8g}, prior range high {self.target:.8g}"
        )


# Higher timeframe -> lower timeframe, period seconds.
PAIRS = {
    "D/H1": (("day", 1, 86400), ("hour", 1, 3600)),
    "H4/M15": (("hour", 4, 14400), ("minute", 15, 900)),
    "H1/M5": (("hour", 1, 3600), ("minute", 5, 300)),
    "M15/M1": (("minute", 15, 900), ("minute", 1, 60)),
}


def parse_closed_candles(payload: dict, *, period: int, now: float) -> list[Candle]:
    raw = payload["data"]["attributes"]["ohlcv_list"]
    if not isinstance(raw, list):
        raise ValueError("missing candles")
    candles = []
    for item in raw:
        if not isinstance(item, list) or len(item) < 6:
            raise ValueError("invalid candle")
        start, opened, high, low, close, volume = item[:6]
        start = int(start)
        opened, high, low, close, volume = map(float, (opened, high, low, close, volume))
        if (
            not all(math.isfinite(x) for x in (opened, high, low, close, volume))
            or low <= 0 or volume < 0 or high < max(opened, close, low)
            or low > min(opened, close)
        ):
            raise ValueError("invalid candle values")
        if start + period <= now:
            candles.append(Candle(start, opened, high, low, close, volume))
    candles.sort(key=lambda bar: bar.start)
    if len({bar.start for bar in candles}) != len(candles):
        raise ValueError("duplicate candles")
    return candles


def assess_structure(
    higher: list[Candle], lower: list[Candle], *, pair: str, now: float,
) -> StructureEvidence | None:
    if pair not in PAIRS:
        raise ValueError("unsupported timeframe pairing")
    high_period = PAIRS[pair][0][2]
    low_period = PAIRS[pair][1][2]
    if len(higher) < 9 or len(lower) < 8:
        return None
    if now - (higher[-1].start + high_period) > high_period * 2:
        return None
    if now - (lower[-1].start + low_period) > low_period * 2:
        return None
    # No interpolation: absent bars make the order of events ambiguous.
    if any(b.start - a.start != high_period for a, b in zip(higher[-9:], higher[-8:])):
        return None
    if any(b.start - a.start != low_period for a, b in zip(lower[-8:], lower[-7:])):
        return None
    prior = higher[-9:-1]
    sweep = higher[-1]
    box_low = min(bar.low for bar in prior)
    box_high = max(bar.high for bar in prior)
    if not (sweep.low < box_low * 0.995 and sweep.close > box_low):
        return None
    if lower[-1].start < sweep.start:
        return None
    break_level = max(bar.high for bar in lower[-8:-5])
    break_bars = lower[-5:-2]
    if not any(bar.close > break_level for bar in break_bars):
        return None
    retest = lower[-2:]
    if not any(bar.low <= break_level * 1.01 for bar in retest):
        return None
    latest = lower[-1]
    if latest.close <= break_level:
        return None
    invalidation = sweep.low
    reward = box_high - latest.close
    risk = latest.close - invalidation
    if risk <= 0 or reward / risk < 1.5:
        return None
    return StructureEvidence(pair, box_low, box_high, sweep.low, break_level,
                             latest.close, invalidation, box_high)


class MarketStructureScanner:
    """Optional, read-only pool candle lookup; errors never create a signal."""

    def __init__(self, *, cache_seconds: float = 300) -> None:
        self.cache_seconds = cache_seconds
        self._cache: dict[tuple[str, str, str], tuple[float, list[Candle] | None]] = {}
        self._request_times: list[float] = []
        self._exit_pending: dict[tuple[str, str], asyncio.Task] = {}
        self._api_key = os.getenv("GECKOTERMINAL_API_KEY") or None

    def exit_research(self, *, pool: str, mint: str) -> dict | None:
        """Refresh in background so candle research cannot delay a live risk exit."""
        key = (pool, mint)
        task = self._exit_pending.get(key)
        if task is not None and task.done():
            if not task.cancelled():
                task.exception()  # Observe failures; cached data still must pass freshness checks.
            self._exit_pending.pop(key, None)
        cached = self._cache.get((pool, mint, "minute:1"))
        if (not cached or time.monotonic() - cached[0] >= 60) and key not in self._exit_pending:
            self._exit_pending[key] = asyncio.create_task(self.scan_exit(pool=pool, mint=mint))
        return assess_dynamic_exit(cached[1] or [], now=time.time()) if cached else None

    async def scan_exit(self, *, pool: str, mint: str) -> dict | None:
        """Closed one-minute candles; research evidence, never an execution order."""
        candles = await self._closed_minute_candles(pool=pool, mint=mint)
        return assess_dynamic_exit(candles or [], now=time.time())

    async def entry_candle_confirmation(self, *, pool: str, mint: str) -> dict | None:
        """Closed one-minute candles; research evidence for an entry, never an order."""
        candles = await self._closed_minute_candles(pool=pool, mint=mint)
        return assess_bullish_continuation_candle(candles or [], now=time.time())

    async def _closed_minute_candles(self, *, pool: str, mint: str) -> list[Candle] | None:
        key = (pool, mint, "minute:1")
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and now - cached[0] < 60:
            return cached[1]
        self._request_times = [t for t in self._request_times if now - t < 60]
        if not pool or not mint or len(self._request_times) >= 8:
            return None
        self._request_times.append(now)
        try:
            candles = await asyncio.to_thread(self._fetch, pool, mint, ("minute", 1, 60))
        except (OSError, ValueError, KeyError, TypeError, TimeoutError):
            candles = None
        self._cache[key] = (now, candles)
        return candles

    def _fetch(self, pool: str, mint: str, frame: tuple[str, int, int]) -> list[Candle] | None:
        timeframe, aggregate, period = frame
        params = urllib.parse.urlencode(
            {"aggregate": aggregate, "limit": 32, "currency": "usd",
             "token": mint, "include_empty_intervals": "true"}
        )
        path = (
            "/networks/solana/pools/" + urllib.parse.quote(pool, safe="")
            + "/ohlcv/" + timeframe + "?" + params
        )
        try:
            payload = self._request_ohlcv(
                "https://api.geckoterminal.com/api/v2" + path, api_key=None
            )
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or not self._api_key:
                raise
            # Free API's rate limit hit: fall back to the paid one this once.
            payload = self._request_ohlcv(
                COINGECKO_PRO_ONCHAIN_BASE_URL + path, api_key=self._api_key
            )
        metadata = payload.get("meta") or {}
        addresses = [
            str((metadata.get(side) or {}).get("address") or "")
            for side in ("base", "quote")
        ]
        if mint not in addresses:
            return None
        return parse_closed_candles(payload, period=period, now=time.time())

    def _request_ohlcv(self, url: str, *, api_key: str | None) -> dict:
        headers = {"Accept": "application/json"}
        if api_key:
            headers["x-cg-pro-api-key"] = api_key
        request = urllib.request.Request(url, headers=headers)
        context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(request, timeout=5, context=context) as response:
            return dict(json.load(response))

    async def scan(self, *, pool: str, mint: str, age_seconds: float) -> StructureEvidence | None:
        if not pool or not mint or age_seconds < 3 * 3600:
            return None
        pair = ("D/H1" if age_seconds >= 14 * 86400 else
                "H4/M15" if age_seconds >= 3 * 86400 else "H1/M5" if age_seconds >= 12 * 3600 else "M15/M1")
        frames = PAIRS[pair]
        readings = []
        for frame in frames:
            key = (pool, mint, f"{frame[0]}:{frame[1]}")
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] < self.cache_seconds:
                candles = cached[1]
            else:
                now = time.monotonic()
                self._request_times = [t for t in self._request_times if now - t < 60]
                if len(self._request_times) >= 8:
                    return None
                self._request_times.append(now)
                try:
                    candles = await asyncio.to_thread(self._fetch, pool, mint, frame)
                except (OSError, ValueError, KeyError, TypeError, TimeoutError):
                    candles = None
                self._cache[key] = (time.monotonic(), candles)
            if candles is None:
                return None
            readings.append(candles)
        return assess_structure(readings[0], readings[1], pair=pair, now=time.time())


def assess_bullish_continuation_candle(
    candles: list[Candle], *, now: float, period: int = 60,
    min_body_ratio: float = 0.5,
) -> dict | None:
    """Latest closed 1m candle: a long bullish body whose lower wick is no
    bigger than its upper wick - a live-chart pattern a user flagged (CUMINU,
    2026-09-24) as evidence a momentum move still has room to continue,
    versus a candle already being rejected from above."""
    if not candles or period <= 0 or not math.isfinite(now):
        return None
    latest = candles[-1]
    if not 0 <= now - (latest.start + period) <= 2 * period:
        return None
    if (not all(math.isfinite(v) for v in
                (latest.open, latest.high, latest.low, latest.close, latest.volume))
        or latest.low <= 0 or latest.volume < 0
        or latest.low > min(latest.open, latest.close)
        or latest.high < max(latest.open, latest.close)):
        return None
    candle_range = latest.high - latest.low
    body = latest.close - latest.open
    if candle_range <= 0 or body <= 0 or body / candle_range < min_body_ratio:
        return None
    upper_wick = latest.high - latest.close
    lower_wick = latest.open - latest.low
    if lower_wick > upper_wick:
        return None
    return {
        "status": "RESEARCH_ONLY", "pattern": "bullish_continuation",
        "as_of": latest.start + period, "body_ratio": body / candle_range,
        "upper_wick": upper_wick, "lower_wick": lower_wick,
        "note": "Unvalidated evidence; one candlestick shape, not a standalone trade signal.",
    }


def assess_dynamic_exit(
    candles: list[Candle], *, now: float, period: int = 60,
    half_life_seconds: float = 600,
) -> dict | None:
    """Research-only time/volatility rule. Half-life is evidence decay, not token life."""
    if (len(candles) < 20 or period <= 0 or half_life_seconds <= 0
        or not math.isfinite(now) or not math.isfinite(half_life_seconds)):
        return None
    bars = candles[-32:]
    if not 0 <= now - (bars[-1].start + period) <= 2 * period:
        return None
    if any(b.start - a.start != period for a, b in zip(bars, bars[1:])):
        return None
    for bar in bars:
        if (not all(math.isfinite(v) for v in
                    (bar.open, bar.high, bar.low, bar.close, bar.volume))
            or bar.low <= 0 or bar.volume < 0
            or bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close)):
            return None
    # Exclude the current bar from the reference range and volatility estimate.
    prior, latest = bars[:-1], bars[-1]
    ranges = [max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
              for a, b in zip(prior, prior[1:])][-14:]
    atr = sum(ranges) / len(ranges)
    if atr <= 0:
        return None
    peak_index = max(range(len(prior)), key=lambda i: prior[i].high)
    peak = prior[peak_index].high
    dwell = 0
    for bar in reversed(prior):
        if peak - bar.close > atr:
            break
        dwell += period
    # As a plateau ages, tighten from 3 ATR toward 1.5 ATR, never below it.
    weight = 2 ** (-dwell / half_life_seconds)
    distance = atr * (1.5 + 1.5 * weight)
    trailing_level = peak - distance
    weights = [2 ** (-(latest.start - b.start) / half_life_seconds) for b in prior]
    pivot = sum(b.close * w for b, w in zip(prior, weights)) / sum(weights)
    peaks = [i for i in range(1, len(prior) - 1)
             if prior[i].high > prior[i - 1].high and prior[i].high >= prior[i + 1].high]
    triple_top = False
    failed_retest = False
    neckline = None
    if len(peaks) >= 3:
        a, b, c = peaks[-3:]
        tops = [prior[i].high for i in (a, b, c)]
        if b - a >= 3 and c - b >= 3 and max(tops) - min(tops) <= atr:
            neckline = min(x.low for x in prior[a:c + 1])
            breaks = [i for i in range(c + 1, len(prior)) if prior[i].close < neckline]
            triple_top = latest.close < neckline
            previous = bars[-2]
            failed_retest = bool(breaks and breaks[0] < len(prior) - 1
                and latest.high >= neckline - atr * 0.25
                and latest.close < neckline and latest.close < latest.open
                and previous.close > previous.open
                and latest.open >= previous.close and latest.close <= previous.open)
    bearish = latest.close < latest.open and latest.close < prior[-1].close
    volume_expansion = latest.volume > sum(b.volume for b in prior[-5:]) / 5
    action = "HOLD_REVIEW"
    if triple_top and failed_retest:
        action = "EXIT_REVIEW"
    elif latest.close < trailing_level and bearish:
        action = "REDUCE_REVIEW"
    elif (latest.close > peak and latest.close > latest.open and volume_expansion):
        action = "ADD_WATCH"
    return {
        "status": "RESEARCH_ONLY", "action": action,
        "as_of": latest.start + period, "period_seconds": period,
        "atr": atr, "pivot": pivot, "reference_peak": peak,
        "plateau_seconds": dwell, "half_life_seconds": half_life_seconds,
        "evidence_weight": weight, "trailing_level": trailing_level,
        "triple_top_break": triple_top, "neckline": neckline,
        "failed_retest_bearish_engulfing": failed_retest,
        "note": "Unvalidated evidence; no automatic trade or net-profit inference."
    }


def classify_candle_pattern(
    candles: list[Candle], *, now: float, period: int = 60, trend_bars: int = 5,
) -> dict:
    """Name the latest closed candle's shape, for research tagging only.

    Shape thresholds (fractions of the candle's full range): doji body <= 10%,
    marubozu body >= 90%, hammer-type lower wick >= 2x body with upper wick
    <= 25%, inverted-type the mirror image, spinning top body <= 30% with both
    wicks >= 25%. Hammer vs hanging man and inverted hammer vs shooting star
    are the same shapes; the prior trend (close trend_bars back vs this open)
    decides which. Always returns a label, so a signal whose candle could not
    be read is counted as 'unavailable' instead of silently dropping out of
    the comparison.
    """
    def result(pattern: str, **extra: object) -> dict:
        return {"pattern": pattern, **extra}

    if not candles or period <= 0 or not math.isfinite(now):
        return result("unavailable")
    latest = candles[-1]
    if not 0 <= now - (latest.start + period) <= 2 * period:
        return result("unavailable")
    o, h, low, c = latest.open, latest.high, latest.low, latest.close
    if (not all(math.isfinite(v) for v in (o, h, low, c)) or low <= 0
            or low > min(o, c) or h < max(o, c)):
        return result("unavailable")
    span = h - low
    if span <= 0:
        return result("flat")
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - low
    b, u, lo = body / span, upper / span, lower / span

    prior = candles[-1 - trend_bars] if len(candles) > trend_bars else None
    trend = (
        "unknown" if prior is None
        else "up" if o > prior.close else "down" if o < prior.close else "flat"
    )
    shape = dict(body_ratio=b, upper_wick_ratio=u, lower_wick_ratio=lo, trend=trend)

    if b <= 0.10:
        if u <= 0.10 and lo >= 0.60:
            return result("dragonfly_doji", **shape)
        if lo <= 0.10 and u >= 0.60:
            return result("gravestone_doji", **shape)
        if u >= 0.30 and lo >= 0.30:
            return result("long_legged_doji", **shape)
        return result("doji", **shape)
    if b >= 0.90:
        return result("bullish_marubozu" if c > o else "bearish_marubozu", **shape)
    if lower >= 2 * body and u <= 0.25:
        name = {"down": "hammer", "up": "hanging_man"}.get(trend, "hammer_shape")
        return result(name, **shape)
    if upper >= 2 * body and lo <= 0.25:
        name = {"down": "inverted_hammer", "up": "shooting_star"}.get(
            trend, "inverted_hammer_shape"
        )
        return result(name, **shape)
    if b <= 0.30 and u >= 0.25 and lo >= 0.25:
        return result("spinning_top", **shape)
    return result("bullish" if c > o else "bearish", **shape)
