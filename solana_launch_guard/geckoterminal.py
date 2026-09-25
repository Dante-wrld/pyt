"""GeckoTerminal client for discovering established Solana tokens that are
regaining momentum well after launch.

Neither pump.fun (launch-time only) nor LaunchLab/Bitquery (launch-time and
the first few minutes of trades) ever re-scan a token later in its life, and
DexScreener's token-profiles/recent-updates feed (tried first, see
launch_guard_solana_momentum.py's docstring) turned out to skew toward
newly-promoted profiles rather than quietly-resurging older ones - confirmed
live 2026-09-23: 0 of 25 sampled were more than 3 days old. GeckoTerminal's
trending_pools endpoint is a genuine "what's moving right now" ranking
(confirmed live the same day: a pool created 11 days earlier still ranked in
it), free and keyless, so this replaces DexScreener as this feed's source.

No OAuth/API key: this is GeckoTerminal's public, unauthenticated API. It has
no documented rate limit or SLA, so callers must treat any failure as routine
and back off via the caller's own poll interval, the same as every other
external feed in this codebase.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import certifi

from .market import MarketQuote

GECKOTERMINAL_BASE_URL = "https://api.geckoterminal.com/api/v2"
# A paid CoinGecko key (GECKOTERMINAL_API_KEY) is only a fallback for when
# the free, unauthenticated, undocumented-rate-limit API above returns a
# 429 - the free API is still tried first on every call. Same paths and
# query params past this prefix, per CoinGecko's own docs.
COINGECKO_PRO_ONCHAIN_BASE_URL = "https://pro-api.coingecko.com/api/v3/onchain"


@dataclass(frozen=True, slots=True)
class GeckoPool:
    """One Solana pool from GeckoTerminal's trending_pools - already
    aggregated (price, liquidity, buys/sells, volume, price change) server
    side, unlike LaunchLab's raw trades, so no local windowing is needed."""

    mint: str
    symbol: str
    price_usd: float
    liquidity_usd: float
    market_cap_usd: float | None
    buys_m5: int
    sells_m5: int
    volume_m5_usd: float
    price_change_m5_pct: float | None
    pool_created_at: str


class GeckoTerminalError(RuntimeError):
    """A GeckoTerminal request failed - HTTP error or a malformed payload."""


class GeckoTerminalClient:
    def __init__(self, *, api_key: str | None = None) -> None:
        self._ssl = ssl.create_default_context(cafile=certifi.where())
        self._api_key = api_key if api_key is not None else os.getenv("GECKOTERMINAL_API_KEY") or None

    async def trending_pools(self, *, pages: int = 1) -> list[GeckoPool]:
        return await asyncio.to_thread(self._trending_pools, pages)

    def _trending_pools(self, pages: int) -> list[GeckoPool]:
        results: list[GeckoPool] = []
        for page in range(1, max(1, pages) + 1):
            payload = self._get(f"/networks/solana/trending_pools?page={page}")
            for row in payload.get("data") or []:
                parsed = _parse_pool(row)
                if parsed is not None:
                    results.append(parsed)
        return results

    def _get(self, path: str) -> dict[str, Any]:
        """The free API first; only on a rate limit (HTTP 429), and only if a
        paid key is configured, retry once against CoinGecko's paid onchain
        API. The free API stays the default even when a key is set."""
        try:
            return self._request(GECKOTERMINAL_BASE_URL + path, api_key=None)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and self._api_key:
                return self._request_or_raise(
                    COINGECKO_PRO_ONCHAIN_BASE_URL + path, api_key=self._api_key
                )
            raise self._as_error(exc) from exc
        except (OSError, ValueError) as exc:
            raise GeckoTerminalError(f"GeckoTerminal request failed: {exc}") from exc

    def _request_or_raise(self, url: str, *, api_key: str) -> dict[str, Any]:
        try:
            return self._request(url, api_key=api_key)
        except urllib.error.HTTPError as exc:
            raise self._as_error(exc) from exc
        except (OSError, ValueError) as exc:
            raise GeckoTerminalError(f"GeckoTerminal request failed: {exc}") from exc

    def _request(self, url: str, *, api_key: str | None) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "solana-launch-guard/0.7",
        }
        if api_key:
            headers["x-cg-pro-api-key"] = api_key
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=20, context=self._ssl) as response:
            return dict(json.load(response))

    @staticmethod
    def _as_error(exc: urllib.error.HTTPError) -> GeckoTerminalError:
        body = exc.read().decode("utf-8", "replace")
        return GeckoTerminalError(f"GeckoTerminal HTTP {exc.code}: {body}")


def _parse_pool(row: dict[str, Any]) -> GeckoPool | None:
    try:
        attributes = row["attributes"]
        base_token_id = str(row["relationships"]["base_token"]["data"]["id"])
        mint = base_token_id.split("_", 1)[1] if "_" in base_token_id else base_token_id
        name = str(attributes.get("name") or "")
        symbol = name.split("/")[0].strip() if "/" in name else name.strip()
        price_usd = float(attributes["base_token_price_usd"])
        liquidity_usd = float(attributes["reserve_in_usd"])
        # fdv_usd (fully-diluted, total supply) fills in when market_cap_usd
        # (circulating supply) is null, which GeckoTerminal does often for
        # meme-style tokens with no verified circulating-supply figure - an
        # overstatement is still usable for the market-cap/liquidity ratio
        # CoinIntelligence.score gates on, an absence isn't.
        market_cap_raw = attributes.get("market_cap_usd") or attributes.get("fdv_usd")
        market_cap_usd = float(market_cap_raw) if market_cap_raw is not None else None
        transactions_m5 = (attributes.get("transactions") or {}).get("m5") or {}
        buys_m5 = int(transactions_m5.get("buys") or 0)
        sells_m5 = int(transactions_m5.get("sells") or 0)
        volume_m5_usd = float((attributes.get("volume_usd") or {}).get("m5") or 0)
        change_m5_raw = (attributes.get("price_change_percentage") or {}).get("m5")
        price_change_m5_pct = float(change_m5_raw) if change_m5_raw is not None else None
        pool_created_at = str(attributes["pool_created_at"])
        if not mint or not symbol or price_usd <= 0:
            return None
        return GeckoPool(
            mint=mint,
            symbol=symbol,
            price_usd=price_usd,
            liquidity_usd=liquidity_usd,
            market_cap_usd=market_cap_usd,
            buys_m5=buys_m5,
            sells_m5=sells_m5,
            volume_m5_usd=volume_m5_usd,
            price_change_m5_pct=price_change_m5_pct,
            pool_created_at=pool_created_at,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _parse_geckoterminal_time(value: str) -> float | None:
    try:
        return (
            datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=datetime.timezone.utc)
            .timestamp()
        )
    except (ValueError, TypeError):
        return None


def build_gecko_quotes(pools: list[GeckoPool]) -> dict[str, MarketQuote]:
    """Convert trending pools into the same MarketQuote shape every other
    feed produces - no age filtering here (that's the caller's policy, see
    SolanaMomentumFeedMixin), just parsing. A pool whose creation time can't
    be parsed is dropped rather than assumed old enough or too new."""
    quotes: dict[str, MarketQuote] = {}
    for pool in pools:
        created_ts = _parse_geckoterminal_time(pool.pool_created_at)
        if created_ts is None:
            continue
        quotes[pool.mint] = MarketQuote(
            mint=pool.mint,
            symbol=pool.symbol,
            price_sol=0.0,
            liquidity_usd=pool.liquidity_usd,
            market_cap_usd=pool.market_cap_usd,
            pair_address="",
            pair_created_at_ms=int(created_ts * 1000),
            buys_m5=pool.buys_m5,
            sells_m5=pool.sells_m5,
            volume_m5_usd=pool.volume_m5_usd,
            price_change_m5_pct=pool.price_change_m5_pct,
            chain="solana",
            price_usd=pool.price_usd,
        )
    return quotes
