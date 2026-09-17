from __future__ import annotations

import asyncio
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

import certifi

WSOL_MINT = "So11111111111111111111111111111111111111112"


@dataclass(frozen=True, slots=True)
class MarketQuote:
    mint: str
    symbol: str
    price_sol: float
    liquidity_usd: float | None
    market_cap_usd: float | None
    pair_address: str


class DexScreenerOracle:
    """Small, cached client for DEX Screener's public token-pairs endpoint."""

    def __init__(self, cache_seconds: float = 2.0) -> None:
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, MarketQuote | None]] = {}
        self._ssl = ssl.create_default_context(cafile=certifi.where())

    async def quote(self, mint: str) -> MarketQuote | None:
        cached = self._cache.get(mint)
        now = time.monotonic()
        if cached and now - cached[0] <= self.cache_seconds:
            return cached[1]
        result = await asyncio.to_thread(self._fetch, mint)
        self._cache[mint] = (now, result)
        return result

    def _fetch(self, mint: str) -> MarketQuote | None:
        encoded = urllib.parse.quote(mint, safe="")
        url = f"https://api.dexscreener.com/latest/dex/tokens/{encoded}"
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "solana-launch-guard/0.2"},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=10, context=self._ssl
            ) as response:
                payload = json.load(response)
        except (OSError, ValueError, urllib.error.URLError):
            return None

        pairs = payload.get("pairs") or []
        candidates: list[dict[str, Any]] = []
        for pair in pairs:
            if pair.get("chainId") != "solana":
                continue
            quote_token = pair.get("quoteToken") or {}
            if quote_token.get("address") != WSOL_MINT:
                continue
            try:
                price = float(pair.get("priceNative"))
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            candidates.append(pair)

        if not candidates:
            return None

        def liquidity(pair: dict[str, Any]) -> float:
            try:
                return float((pair.get("liquidity") or {}).get("usd") or 0)
            except (TypeError, ValueError):
                return 0.0

        pair = max(candidates, key=liquidity)
        try:
            price_sol = float(pair["priceNative"])
        except (KeyError, TypeError, ValueError):
            return None

        market_cap = pair.get("marketCap") or pair.get("fdv")
        try:
            market_cap_usd = float(market_cap) if market_cap is not None else None
        except (TypeError, ValueError):
            market_cap_usd = None

        return MarketQuote(
            mint=mint,
            symbol=str((pair.get("baseToken") or {}).get("symbol") or mint[:6]),
            price_sol=price_sol,
            liquidity_usd=liquidity(pair) or None,
            market_cap_usd=market_cap_usd,
            pair_address=str(pair.get("pairAddress") or ""),
        )
