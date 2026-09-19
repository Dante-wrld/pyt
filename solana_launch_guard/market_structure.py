from __future__ import annotations

import asyncio
import json
import math
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

import certifi


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

    def _fetch(self, pool: str, mint: str, frame: tuple[str, int, int]) -> list[Candle] | None:
        timeframe, aggregate, period = frame
        params = urllib.parse.urlencode(
            {"aggregate": aggregate, "limit": 32, "currency": "usd",
             "token": mint, "include_empty_intervals": "true"}
        )
        url = (
            "https://api.geckoterminal.com/api/v2/networks/solana/pools/"
            + urllib.parse.quote(pool, safe="")
            + "/ohlcv/" + timeframe + "?" + params
        )
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(request, timeout=5, context=context) as response:
            payload = json.load(response)
        metadata = payload.get("meta") or {}
        addresses = [
            str((metadata.get(side) or {}).get("address") or "")
            for side in ("base", "quote")
        ]
        if mint not in addresses:
            return None
        return parse_closed_candles(payload, period=period, now=time.time())

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
                if len(self._request_times) >= 20:
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
