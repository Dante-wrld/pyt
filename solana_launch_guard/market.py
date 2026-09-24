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
    chain: str = "solana"
    price_usd: float | None = None

    @property
    def recommendation_price(self) -> float:
        if self.price_usd is not None and self.price_usd > 0:
            return self.price_usd
        return self.price_sol

    @property
    def recommendation_currency(self) -> str:
        return "USD" if self.price_usd is not None else "SOL"

    @property
    def recommendation_key(self) -> str:
        address = self.mint if self.chain == "solana" else self.mint.lower()
        return f"{self.chain}:{address}"

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

    def __init__(self, cache_seconds: float = 8.0, max_429_retries: int = 2) -> None:
        # 2.0 did almost nothing to cut duplicate requests for the same mint
        # within one process; 8.0 is still comfortably under the 15s
        # freshness bound live buy decisions require, but cuts repeat calls
        # for a mint queried more than once in quick succession.
        self.cache_seconds = cache_seconds
        # A missed quote costs very different amounts depending on the
        # caller: the live trial's own buy/sell decisions run over a
        # handful of mints at a time, so retrying a 429 there can be the
        # difference between seeing a real trade and missing it. The bulk
        # portfolio/watchlist scan queries dozens of mints (observed: ~75
        # owned holdings) through a small concurrency semaphore, so every
        # retry's backoff sleep is paid by that many sequential batches -
        # missing one mint's quote for a single ~30s cycle there is cheap,
        # but the retries compounding across a whole scan is what was
        # actually pushing the portfolio snapshot stale enough to block
        # live buy/sell decisions that depend on its freshness. Callers
        # doing that kind of bulk scan should construct with retries=0.
        self._max_429_retries = max_429_retries
        self._cache: dict[str, tuple[float, MarketQuote | None]] = {}
        self._stock_token_cache: (
            tuple[float, frozenset[str], frozenset[str]] | None
        ) = None
        self._ssl = ssl.create_default_context(cafile=certifi.where())

    async def quote(
        self, mint: str, *, chain: str = "solana"
    ) -> MarketQuote | None:
        normalized = mint if chain == "solana" else mint.lower()
        cache_key = f"{chain}:{normalized}"
        cached = self._cache.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] <= self.cache_seconds:
            return cached[1]
        result = await asyncio.to_thread(self._fetch, mint, chain)
        self._cache[cache_key] = (now, result)
        return result

    async def quote_many(
        self, mints: list[str], *, chain: str = "solana"
    ) -> dict[str, MarketQuote | None]:
        """Batched quote lookup, for a bulk scan over many mints at once
        (the portfolio monitor's own holdings, currently ~75 and growing).

        DexScreener's public tokens/v1/{chain}/{addresses} endpoint accepts
        up to 30 comma-separated addresses per request and sits on a
        separate, much less restrictive rate-limit bucket than the
        single-token /latest/dex/tokens/{mint} endpoint _fetch/quote use -
        confirmed live 2026-09-24: the single-token endpoint was returning
        429 on every request from this machine's combined polling volume
        (portfolio scan + the live trial's own quote checks), while
        tokens/v1 served the exact same pair data cleanly at the same time.
        Batching ~75 individual requests into ~3 cuts both the request
        count and the 429 exposure by roughly the same factor.

        Reuses the same per-mint cache quote() does, so a mint already
        fresh from an earlier call (batched or not) is served without a
        network call, and every mint this fetches is cached afterward for
        any other caller (e.g. the live trial's own per-position checks).
        """
        results: dict[str, MarketQuote | None] = {}
        now = time.monotonic()
        to_fetch: list[str] = []
        for mint in mints:
            normalized = mint if chain == "solana" else mint.lower()
            cache_key = f"{chain}:{normalized}"
            cached = self._cache.get(cache_key)
            if cached and now - cached[0] <= self.cache_seconds:
                results[mint] = cached[1]
            else:
                to_fetch.append(mint)
        for start in range(0, len(to_fetch), 30):
            batch = to_fetch[start:start + 30]
            pairs = await asyncio.to_thread(self._request_tokens_batch, batch, chain)
            by_mint: dict[str, list[dict[str, Any]]] = {mint: [] for mint in batch}
            for pair in pairs:
                base_address = str((pair.get("baseToken") or {}).get("address") or "")
                for mint in batch:
                    matches = (
                        base_address == mint if chain == "solana"
                        else base_address.casefold() == mint.casefold()
                    )
                    if matches:
                        by_mint[mint].append(pair)
            for mint in batch:
                quote = self._select_quote(mint, by_mint[mint], chain)
                results[mint] = quote
                normalized = mint if chain == "solana" else mint.lower()
                self._cache[f"{chain}:{normalized}"] = (now, quote)
        return results

    async def discover_token_profiles(self, chain: str) -> tuple[str, ...]:
        return await asyncio.to_thread(self._discover_token_profiles, chain)

    async def robinhood_stock_token_addresses(
        self,
    ) -> frozenset[str] | None:
        return await asyncio.to_thread(self._robinhood_stock_token_addresses)

    async def robinhood_stock_token_symbols(
        self,
    ) -> frozenset[str] | None:
        return await asyncio.to_thread(self._robinhood_stock_token_symbols)

    async def sol_usd_price(self) -> float | None:
        return await asyncio.to_thread(self._fetch_sol_usd)

    def _request_token(self, mint: str) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(mint, safe="")
        url = f"https://api.dexscreener.com/latest/dex/tokens/{encoded}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "solana-launch-guard/0.7",
            },
        )
        # A 429 (observed live: this app's polling volume across its several
        # concurrent processes exceeds DexScreener's public rate limit) used
        # to be swallowed identically to "this token genuinely has no
        # pairs" - indistinguishable downstream, and every caller (hunter-v1's
        # own exit review, the portfolio-v1 exit path, the watchlist scan)
        # would just see a permanent "no quote" for that mint until the next
        # request happened to land outside the rate-limited window. A couple
        # of short backoff retries clears most of these without hammering
        # the API further.
        for attempt in range(self._max_429_retries + 1):
            try:
                with urllib.request.urlopen(
                    request, timeout=10, context=self._ssl
                ) as response:
                    payload = json.load(response)
                return list(payload.get("pairs") or [])
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < self._max_429_retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return []
            except (OSError, ValueError, urllib.error.URLError):
                return []
        return []

    def _request_tokens_batch(
        self, mints: list[str], chain: str
    ) -> list[dict[str, Any]]:
        encoded = ",".join(urllib.parse.quote(mint, safe="") for mint in mints)
        url = f"https://api.dexscreener.com/tokens/v1/{chain}/{encoded}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "solana-launch-guard/0.7",
            },
        )
        for attempt in range(self._max_429_retries + 1):
            try:
                with urllib.request.urlopen(
                    request, timeout=10, context=self._ssl
                ) as response:
                    payload = json.load(response)
                return list(payload) if isinstance(payload, list) else []
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < self._max_429_retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return []
            except (OSError, ValueError, urllib.error.URLError):
                return []
        return []

    def _request_json(self, url: str) -> Any:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "solana-launch-guard/0.7",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=10, context=self._ssl
            ) as response:
                return json.load(response)
        except (OSError, ValueError, urllib.error.URLError):
            return None

    def _discover_token_profiles(self, chain: str) -> tuple[str, ...]:
        endpoints = (
            "https://api.dexscreener.com/token-profiles/latest/v1",
            "https://api.dexscreener.com/token-profiles/recent-updates/v1",
        )
        discovered: dict[str, str] = {}
        for endpoint in endpoints:
            payload = self._request_json(endpoint)
            if not isinstance(payload, list):
                continue
            for profile in payload:
                if not isinstance(profile, dict):
                    continue
                if str(profile.get("chainId") or "") != chain:
                    continue
                address = str(profile.get("tokenAddress") or "").strip()
                if not address:
                    continue
                key = address if chain == "solana" else address.lower()
                discovered.setdefault(key, address)
        return tuple(discovered.values())

    def _robinhood_stock_token_addresses(self) -> frozenset[str] | None:
        stock_tokens = self._robinhood_stock_tokens()
        return stock_tokens[0] if stock_tokens is not None else None

    def _robinhood_stock_token_symbols(self) -> frozenset[str] | None:
        stock_tokens = self._robinhood_stock_tokens()
        return stock_tokens[1] if stock_tokens is not None else None

    def _robinhood_stock_tokens(
        self,
    ) -> tuple[frozenset[str], frozenset[str]] | None:
        now = time.monotonic()
        if self._stock_token_cache is not None:
            cached_at, addresses, symbols = self._stock_token_cache
            if now - cached_at <= 600:
                return addresses, symbols

        payload = self._request_json("https://api.robinhood.com/rhj/assets")
        excluded: set[str] = set()
        collected_symbols: set[str] = set()
        assets = payload.get("assets") if isinstance(payload, dict) else None
        if not isinstance(assets, list):
            if self._stock_token_cache is not None:
                return (
                    self._stock_token_cache[1],
                    self._stock_token_cache[2],
                )
            return None
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            symbol = str(asset.get("tokenSymbol") or "").strip().casefold()
            if symbol:
                collected_symbols.add(symbol)
            deployments = asset.get("deployments")
            if not isinstance(deployments, list):
                continue
            for deployment in deployments:
                if not isinstance(deployment, dict):
                    continue
                try:
                    chain_id = int(deployment.get("chainId") or 0)
                except (TypeError, ValueError):
                    continue
                if chain_id != 4663:
                    continue
                address = str(
                    deployment.get("contractAddress") or ""
                ).strip()
                if address:
                    excluded.add(address.casefold())
        result = frozenset(excluded)
        stock_symbols = frozenset(collected_symbols)
        self._stock_token_cache = (now, result, stock_symbols)
        return result, stock_symbols

    def _fetch(self, mint: str, chain: str = "solana") -> MarketQuote | None:
        pairs = self._request_token(mint)
        return self._select_quote(mint, pairs, chain)

    def _select_quote(
        self, mint: str, pairs: list[dict[str, Any]], chain: str = "solana"
    ) -> MarketQuote | None:
        candidates: list[dict[str, Any]] = []
        for pair in pairs:
            if pair.get("chainId") != chain:
                continue
            base_token = pair.get("baseToken") or {}
            quote_token = pair.get("quoteToken") or {}
            base_address = str(base_token.get("address") or "")
            addresses_match = (
                base_address == mint
                if chain == "solana"
                else base_address.casefold() == mint.casefold()
            )
            if not addresses_match:
                continue
            if chain == "solana" and quote_token.get("address") != WSOL_MINT:
                continue
            price_field = "priceNative" if chain == "solana" else "priceUsd"
            raw_price = pair.get(price_field)
            if raw_price is None:
                continue
            try:
                price = float(raw_price)
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
            price_sol = float(pair["priceNative"]) if chain == "solana" else 0.0
            raw_price_usd = pair.get("priceUsd")
            price_usd = (
                float(raw_price_usd) if raw_price_usd is not None else None
            )
        except (KeyError, TypeError, ValueError):
            return None
        if chain != "solana" and (price_usd is None or price_usd <= 0):
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
            chain=chain,
            price_usd=price_usd,
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
        raw_price = pair.get("priceUsd")
        if raw_price is None:
            return None
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None
