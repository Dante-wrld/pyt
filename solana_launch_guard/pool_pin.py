"""Keep each token's quotes on one pool.

DEX Screener's batch endpoint returns a capped number of pairs per request,
so a token's main pool is sometimes missing from a response and a smaller
pool, often at a very different price, becomes the "deepest" one for that
poll. Found 2026-10-01: about 3,800 tracked readings flipped this way (one
token showed $0.0126 / $512k liquidity, then $0.0032 / $209k for one
minute, then back), which booked phantom -73% crashes and gains in the
evaluator and can fire the live hard stop or liquidity-collapse exit on a
healthy position.

`PoolPinner` remembers the pool a token was first quoted on. While that pool
is in the response and at least half as deep as the deepest pool it is
used, so the price stays on one pool. If it is missing the token gets no
quote that poll (callers already treat that as
"try again"). Only after `repin_after` consecutive polls with the pinned
pool missing or under half the deepest one's liquidity does it switch.
That covers real migrations (a pump.fun curve graduating to PumpSwap) and
a first pin that landed on a side pool because the main one was missing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TypeVar

T = TypeVar("T")

REPIN_AFTER_POLLS = 8
WEAK_FRACTION = 0.5
MAX_PINS = 50_000


class PoolPinner:
    def __init__(
        self,
        *,
        repin_after: int = REPIN_AFTER_POLLS,
        weak_fraction: float = WEAK_FRACTION,
    ) -> None:
        if repin_after < 1 or not 0 < weak_fraction <= 1:
            raise ValueError("repin_after must be >= 1 and 0 < weak_fraction <= 1")
        self.repin_after = repin_after
        self.weak_fraction = weak_fraction
        self._pins: dict[str, str] = {}
        self._strikes: dict[str, int] = {}

    def _pin(self, key: str, pair: str) -> None:
        self._pins.pop(key, None)
        self._pins[key] = pair
        self._strikes[key] = 0
        while len(self._pins) > MAX_PINS:  # oldest pins first
            oldest = next(iter(self._pins))
            self.forget(oldest)

    def pinned(self, key: str) -> str | None:
        return self._pins.get(key)

    def choose(
        self,
        key: str,
        candidates: Sequence[T],
        *,
        pair_of: Callable[[T], str],
        liquidity_of: Callable[[T], float],
    ) -> T | None:
        """The candidate to quote `key` from this poll, or None to skip it."""
        if not candidates:
            return None
        deepest = max(candidates, key=liquidity_of)
        pin = self._pins.get(key)
        if pin is None:
            if pair_of(deepest):
                self._pin(key, pair_of(deepest))
            return deepest
        match = next((c for c in candidates if pair_of(c) == pin), None)
        healthy = match is not None and liquidity_of(match) >= (
            liquidity_of(deepest) * self.weak_fraction
        )
        if healthy:
            self._strikes[key] = 0
            return match
        strikes = self._strikes.get(key, 0) + 1
        if strikes >= self.repin_after:
            self._pin(key, pair_of(deepest))
            return deepest
        self._strikes[key] = strikes
        # Present but much shallower: keep the consistent price until a
        # migration is confirmed. Missing: skip this poll.
        return match

    def forget(self, key: str) -> None:
        self._pins.pop(key, None)
        self._strikes.pop(key, None)
