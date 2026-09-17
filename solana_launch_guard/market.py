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
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@dataclass(frozen=True, slots=True)
class MarketQuote:
    mint: str
    symbol: str
    price_sol: float
    liquidity_usd: float | None
    market_cap_usd: float | None
    pair_address: str
    pair_created_at_ms: int | None
    buys_m5: int
    sells_m5: int
    volume_m5_usd: float
    price_change_m5_pct: float | None

    @property
    def market_cap_to_liquidity(self) -> float | None:
        if (
            self.market_cap_usd is None
            or self.liquidity_usd is None
            or self.liquidity_usd <= 0
        ):
            return None
        return self.market_cap_usd / self.liquidity_usd

    @property
    def buy_sell_ratio(self) -> float:
        return self.buys_m5 / max(1, self.sells_m5)

    @property
    def volume_liquidity_ratio(self) -> float | None:
        if self.liquidity_usd is None or self.liquidity_usd <= 0:
            return None
        return self.volume_m5_usd / self.liquidity_usd


class DexScreenerOracle:
    """Cached client for DEX Screener's public token-pairs endpoint."""

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

    async def sol_usd_price(self) -> float | None:
        return await asyncio.to_thread(self._fetch_sol_usd)

    def _request_token(self, mint: str) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(mint, safe="")
        url = f"https://api.dexscreener.com/latest/dex/tokens/{encoded}"
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "solana-launch-guard/0.3"},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=10, context=self._ssl
            ) as response:
                payload = json.load(response)
        except (OSError, ValueError, urllib.error.URLError):
            return []
        return list(payload.get("pairs") or [])

    def _fetch(self, mint: str) -> MarketQuote | None:
        pairs = self._request_token(mint)
        candidates: list[dict[str, Any]] = []
        for pair in pairs:
            if pair.get("chainId") != "solana":
                continue
            base_token = pair.get("baseToken") or {}
            quote_token = pair.get("quoteToken") or {}
            if base_token.get("address") != mint:
                continue
            if quote_token.get("address") != WSOL_MINT:
                continue
            try:
                price = float(pair.get("priceNative"))
            except (TypeError, ValueError):
                continue
            if price > 0:
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

        txns_m5 = ((pair.get("txns") or {}).get("m5") or {})
        volume_m5 = (pair.get("volume") or {}).get("m5") or 0
        price_change_m5 = (pair.get("priceChange") or {}).get("m5")
        pair_created = pair.get("pairCreatedAt")
        try:
            created_ms = int(pair_created) if pair_created is not None else None
        except (TypeError, ValueError):
            created_ms = None

        try:
            change = (
                float(price_change_m5) if price_change_m5 is not None else None
            )
        except (TypeError, ValueError):
            change = None

        return MarketQuote(
            mint=mint,
            symbol=str((pair.get("baseToken") or {}).get("symbol") or mint[:6]),
            price_sol=price_sol,
            liquidity_usd=liquidity(pair) or None,
            market_cap_usd=market_cap_usd,
            pair_address=str(pair.get("pairAddress") or ""),
            pair_created_at_ms=created_ms,
            buys_m5=int(txns_m5.get("buys") or 0),
            sells_m5=int(txns_m5.get("sells") or 0),
            volume_m5_usd=float(volume_m5),
            price_change_m5_pct=change,
        )

    def _fetch_sol_usd(self) -> float | None:
        pairs = self._request_token(WSOL_MINT)
        candidates: list[dict[str, Any]] = []
        for pair in pairs:
            if pair.get("chainId") != "solana":
                continue
            base = pair.get("baseToken") or {}
            quote = pair.get("quoteToken") or {}
            if base.get("address") != WSOL_MINT:
                continue
            if quote.get("address") != USDC_MINT:
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
            price = float(pair.get("priceUsd"))
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None
