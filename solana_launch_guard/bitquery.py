"""Bitquery client for Raydium LaunchLab discovery and live pricing.

LaunchLab is the shared on-chain program behind stonk.fun, LetsBONK.fun,
and any other LaunchLab-based launchpad frontend - watching its program
directly (rather than building a one-off integration per frontend brand)
surfaces all of them at once. Confirmed live 2026-09-23: mint
6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx (symbol STONK) trades under
Bitquery's "raydium_launchpad" protocol, and mint
HTmQz7My6MehV7bjhJ6jde8nDND1yvsz68d24LP7YgUQ (symbol GP) - a real
dust position already sitting in the wallet, unsellable and undiscoverable
by the existing pump.fun-only pipeline - does too.

Wired into the live discovery pipeline via launch_guard_launchlab.py's
LaunchLabFeedMixin, NOT core.py's pump.fun-specific Launch/risk-gate at
all - launch_guard_multichain.py's run_multichain_feed already proves a
non-pump.fun MarketQuote can reach the shared RecommendationBook
(self.recommendations.add(quote, result), scored generically by
CoinIntelligence.score) without ever touching Launch, so this reuses
that exact, already-running pattern instead of adapting the pump.fun
gate. This module covers the confirmed, tested surface: OAuth token
refresh, recent pool creations, recent trades (live price/volume), and
recent pool reserves (live liquidity, in USD on both sides of the pool -
some LaunchLab pools are quoted against a tokenized-stock token rather
than SOL, confirmed live via stonk.fun's "stock-paired" pools, so only
the USD-denominated reserve is used, never assumed to be SOL).

Creator-buy-at-launch (the one pump.fun signal with no LaunchLab
equivalent built here) is not part of MarketQuote/CoinIntelligence's
scoring at all, so this gap doesn't block using the same pipeline -
it's simply not a check this path performs, same as every non-Solana
chain already flowing through run_multichain_feed.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import certifi

from .market import MarketQuote

OAUTH_TOKEN_URL = "https://oauth2.bitquery.io/oauth2/token"
GRAPHQL_URL = "https://streaming.bitquery.io/graphql"

# LaunchLab's own program - shared by every frontend built on it (stonk.fun,
# LetsBONK.fun, vanilla raydium.io/launchpad, and any future one), so this
# one address is the whole discovery surface, not just Raydium's own UI.
LAUNCHLAB_PROGRAM_ADDRESS = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
LAUNCHLAB_PROTOCOL_NAME = "raydium_launchpad"

# A fresh token is requested this many seconds before its reported
# expiry, so a request never starts against a token that expires mid-flight.
_TOKEN_REFRESH_MARGIN_SECONDS = 60

# Every pool-creation sample observed live 2026-09-23 used this exact
# supply (1e15 raw at 6 decimals = 1B tokens) - used as a fallback for
# market-cap estimation when a mint's own creation event isn't in the
# current recent-creations window (it only covers the newest ~N launches,
# not full history), preferring the mint's own recorded value when available.
LAUNCHLAB_STANDARD_SUPPLY = 1_000_000_000.0
_FIVE_MINUTES_SECONDS = 300

# Field-selection fragments shared by the combined snapshot query below -
# kept in one place so the three result shapes and their parsers
# (_parse_pool_creation/_parse_trade/_parse_pool) never drift apart.
_CREATION_FIELDS = """
              Block { Time }
              Transaction { Signer Signature }
              Instruction {
                Accounts { Address Token { Mint Owner } }
                Program {
                  Arguments {
                    Name
                    Value {
                      ... on Solana_ABI_Json_Value_Arg { json }
                    }
                  }
                }
              }
"""

_TRADE_FIELDS = """
              Block { Time }
              Trade {
                Currency { MintAddress Symbol }
                PriceInUSD
                Side { Type AmountInUSD }
              }
"""

_POOL_FIELDS = """
              Block { Time }
              Pool {
                Base { PostAmountInUSD }
                Quote { PostAmountInUSD }
                Market {
                  BaseCurrency { MintAddress Symbol }
                  QuoteCurrency { MintAddress Symbol }
                }
              }
"""


@dataclass(frozen=True, slots=True)
class LaunchLabPoolCreation:
    """One initialize_v2 instruction: a new LaunchLab bonding-curve pool.

    supply/total_base_sell/total_quote_fund_raising are raw integers, in
    the token's own base units and lamports respectively - the same
    curve-shape parameters pump.fun's virtual reserves describe, but
    fixed at creation rather than continuously updated; pair with
    LaunchLabTrade for live price/volume once the mint is known.
    """

    mint: str
    name: str
    symbol: str
    creator: str
    signature: str
    block_time: str
    token_decimals: int
    supply_raw: int
    total_base_sell_raw: int
    total_quote_fund_raising_lamports: int
    migrate_type: int


@dataclass(frozen=True, slots=True)
class LaunchLabTrade:
    """One buy or sell against a LaunchLab bonding curve, from Bitquery's
    DEXTradeByTokens - aggregate a window of these into
    buys_m5/sells_m5/volume_m5_usd/price_change_m5_pct, the same shape
    MarketQuote already provides from DexScreener, for the recommendation
    engine to score."""

    mint: str
    symbol: str
    side: str  # "buy" or "sell"
    price_usd: float
    amount_usd: float
    block_time: str


@dataclass(frozen=True, slots=True)
class LaunchLabPool:
    """A LaunchLab pool's current reserve state, from Bitquery's DEXPools -
    liquidity_usd is both sides of the pool combined, the same convention
    MarketQuote.liquidity_usd already uses. quote_mint/quote_symbol are
    whatever the pool is actually quoted against - not always SOL (stonk.fun
    pairs some launches against a tokenized-stock token instead, confirmed
    live), so callers must never assume SOL."""

    mint: str
    symbol: str
    liquidity_usd: float
    quote_mint: str
    quote_symbol: str
    block_time: str


class BitqueryAuthError(RuntimeError):
    """The OAuth2 client-credentials exchange failed - bad/expired
    credentials, not a query-level problem."""


class BitqueryClient:
    """OAuth2-authenticated GraphQL client, scoped to LaunchLab discovery
    and pricing. Token refresh is automatic and internal - callers never
    handle the access token directly."""

    def __init__(self, client_id: str, client_secret: str) -> None:
        if not client_id or not client_secret:
            raise ValueError("Bitquery client_id and client_secret are required")
        self._client_id = client_id
        self._client_secret = client_secret
        self._ssl = ssl.create_default_context(cafile=certifi.where())
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    async def recent_launchlab_snapshot(
        self, *, creations_limit: int = 20, trades_limit: int = 50, pools_limit: int = 50,
    ) -> tuple[list[LaunchLabPoolCreation], list[LaunchLabTrade], list[LaunchLabPool]]:
        return await asyncio.to_thread(
            self._recent_launchlab_snapshot, creations_limit, trades_limit, pools_limit
        )

    def _access_token(self) -> str:
        now = time.monotonic()
        if self._token is not None and now < self._token_expires_at:
            return self._token
        body = "&".join(
            f"{key}={value}"
            for key, value in (
                ("grant_type", "client_credentials"),
                ("client_id", self._client_id),
                ("client_secret", self._client_secret),
                ("scope", "api"),
            )
        )
        request = urllib.request.Request(
            OAUTH_TOKEN_URL,
            data=body.encode("ascii"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15, context=self._ssl) as response:
                payload = json.load(response)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise BitqueryAuthError(f"Bitquery token exchange failed: {exc}") from exc
        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not isinstance(token, str) or not token or not isinstance(expires_in, (int, float)):
            raise BitqueryAuthError("Bitquery token response is missing access_token/expires_in")
        self._token = token
        self._token_expires_at = now + max(0, expires_in - _TOKEN_REFRESH_MARGIN_SECONDS)
        return token

    def _graphql(self, query: str) -> dict[str, Any]:
        request = urllib.request.Request(
            GRAPHQL_URL,
            data=json.dumps({"query": query}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json",
                "User-Agent": "solana-launch-guard/0.7",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30, context=self._ssl) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            # A non-2xx status (e.g. 402 "points limit: usage quota reached")
            # carries the actual reason in the response body, not in the
            # generic HTTPError message - without reading it, a quota
            # exhaustion looks identical to any other outage in the logs.
            body = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"Bitquery GraphQL HTTP {exc.code}: {body}") from exc
        errors = payload.get("errors")
        if errors:
            raise RuntimeError(f"Bitquery GraphQL error: {errors}")
        return payload.get("data") or {}

    def _recent_launchlab_snapshot(
        self, creations_limit: int, trades_limit: int, pools_limit: int,
    ) -> tuple[list[LaunchLabPoolCreation], list[LaunchLabTrade], list[LaunchLabPool]]:
        # One combined GraphQL request in place of three separate ones -
        # Bitquery bills a flat 5 points per call regardless of row count,
        # so three aliased fields under one Solana block costs the same 5
        # points as any single one of them did alone, cutting LaunchLab's
        # per-poll cost by two-thirds with no change to freshness or data.
        query = f"""
        query {{
          Solana {{
            creations: Instructions(
              limit: {{count: {int(creations_limit)}}}
              orderBy: {{descending: Block_Time}}
              where: {{
                Instruction: {{
                  Program: {{
                    Address: {{is: "{LAUNCHLAB_PROGRAM_ADDRESS}"}}
                    Method: {{is: "initialize_v2"}}
                  }}
                }}
                Transaction: {{Result: {{Success: true}}}}
              }}
            ) {{
              {_CREATION_FIELDS}
            }}
            trades: DEXTradeByTokens(
              limit: {{count: {int(trades_limit)}}}
              orderBy: {{descending: Block_Time}}
              where: {{
                Trade: {{Dex: {{ProtocolName: {{is: "{LAUNCHLAB_PROTOCOL_NAME}"}}}}}}
              }}
            ) {{
              {_TRADE_FIELDS}
            }}
            pools: DEXPools(
              limit: {{count: {int(pools_limit)}}}
              orderBy: {{descending: Block_Time}}
              where: {{
                Pool: {{Dex: {{ProtocolName: {{is: "{LAUNCHLAB_PROTOCOL_NAME}"}}}}}}
              }}
            ) {{
              {_POOL_FIELDS}
            }}
          }}
        }}
        """
        data = self._graphql(query)
        solana = data.get("Solana", {}) or {}
        creations: list[LaunchLabPoolCreation] = []
        for row in solana.get("creations", []) or []:
            parsed_creation = _parse_pool_creation(row)
            if parsed_creation is not None:
                creations.append(parsed_creation)
        trades: list[LaunchLabTrade] = []
        for row in solana.get("trades", []) or []:
            parsed_trade = _parse_trade(row)
            if parsed_trade is not None:
                trades.append(parsed_trade)
        pools: list[LaunchLabPool] = []
        for row in solana.get("pools", []) or []:
            parsed_pool = _parse_pool(row)
            if parsed_pool is not None:
                pools.append(parsed_pool)
        return creations, trades, pools


def _parse_pool_creation(row: dict[str, Any]) -> LaunchLabPoolCreation | None:
    try:
        block_time = row["Block"]["Time"]
        signer = row["Transaction"]["Signer"]
        signature = row["Transaction"]["Signature"]
        arguments = row["Instruction"]["Program"]["Arguments"]
        accounts = row["Instruction"]["Accounts"]
    except (KeyError, TypeError):
        return None
    args_by_name = {arg.get("Name"): arg.get("Value", {}).get("json") for arg in arguments}
    base_mint_param = args_by_name.get("base_mint_param")
    curve_param = args_by_name.get("curve_param")
    if not base_mint_param or not curve_param:
        return None
    try:
        mint_meta = json.loads(base_mint_param)
        curve = json.loads(curve_param)
        curve_data = curve["Constant"]["data"]
    except (ValueError, KeyError, TypeError):
        return None
    # The mint account is the one whose Token.Mint equals its own Address
    # and whose Token.Owner is the SPL Token program - every other entry
    # in Accounts is a different account (creator, vault, curve state,
    # the LaunchLab program itself, ...) with an empty Token sub-object.
    mint = ""
    for account in accounts:
        token = account.get("Token") or {}
        if token.get("Mint") and token.get("Mint") == account.get("Address"):
            mint = token["Mint"]
            break
    if not mint:
        return None
    try:
        return LaunchLabPoolCreation(
            mint=mint,
            name=str(mint_meta.get("name") or "Unknown"),
            symbol=str(mint_meta.get("symbol") or "UNKNOWN"),
            creator=str(signer),
            signature=str(signature),
            block_time=str(block_time),
            token_decimals=int(mint_meta.get("decimals") or 0),
            supply_raw=int(curve_data["supply"]),
            total_base_sell_raw=int(curve_data["total_base_sell"]),
            total_quote_fund_raising_lamports=int(curve_data["total_quote_fund_raising"]),
            migrate_type=int(curve_data.get("migrate_type") or 0),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _parse_trade(row: dict[str, Any]) -> LaunchLabTrade | None:
    try:
        trade = row["Trade"]
        currency = trade["Currency"]
        side = trade["Side"]
        mint = str(currency["MintAddress"])
        price_usd = float(trade["PriceInUSD"])
        amount_usd = float(side["AmountInUSD"])
        side_type = str(side["Type"])
        if not mint or side_type not in {"buy", "sell"}:
            return None
        return LaunchLabTrade(
            mint=mint,
            symbol=str(currency.get("Symbol") or "UNKNOWN"),
            side=side_type,
            price_usd=price_usd,
            amount_usd=amount_usd,
            block_time=str(row["Block"]["Time"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _parse_pool(row: dict[str, Any]) -> LaunchLabPool | None:
    try:
        pool = row["Pool"]
        base_currency = pool["Market"]["BaseCurrency"]
        quote_currency = pool["Market"]["QuoteCurrency"]
        mint = str(base_currency["MintAddress"])
        base_usd = float(pool["Base"]["PostAmountInUSD"])
        quote_usd = float(pool["Quote"]["PostAmountInUSD"])
        if not mint:
            return None
        return LaunchLabPool(
            mint=mint,
            symbol=str(base_currency.get("Symbol") or "UNKNOWN"),
            liquidity_usd=base_usd + quote_usd,
            quote_mint=str(quote_currency.get("MintAddress") or ""),
            quote_symbol=str(quote_currency.get("Symbol") or "UNKNOWN"),
            block_time=str(row["Block"]["Time"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _parse_bitquery_time(value: str) -> float | None:
    try:
        return (
            datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=datetime.timezone.utc)
            .timestamp()
        )
    except (ValueError, TypeError):
        return None


def build_launchlab_quotes(
    *, trades: list[LaunchLabTrade], pools: list[LaunchLabPool],
    creations: tuple[LaunchLabPoolCreation, ...] = (), now: float | None = None,
) -> dict[str, MarketQuote]:
    """Combine a batch of trades and pool reserves into MarketQuote objects,
    the same shape CoinIntelligence.score and RecommendationBook.add
    already consume for every other chain (see run_multichain_feed) - no
    Launch/pump.fun-specific adaptation needed. A mint only gets a quote if
    it has BOTH recent trade activity and known pool liquidity; either
    missing means it can't be scored honestly, so it's skipped rather than
    guessed at.
    """
    at = time.time() if now is None else now
    liquidity_by_mint = {p.mint: p.liquidity_usd for p in pools}
    supply_by_mint = {
        c.mint: c.supply_raw / 10**c.token_decimals
        for c in creations if c.token_decimals >= 0 and c.supply_raw > 0
    }
    trades_by_mint: dict[str, list[LaunchLabTrade]] = {}
    for trade in trades:
        trades_by_mint.setdefault(trade.mint, []).append(trade)

    quotes: dict[str, MarketQuote] = {}
    for mint, mint_trades in trades_by_mint.items():
        liquidity_usd = liquidity_by_mint.get(mint)
        if liquidity_usd is None:
            continue
        with_ts = [(t, _parse_bitquery_time(t.block_time)) for t in mint_trades]
        timed = [(t, ts) for t, ts in with_ts if ts is not None]
        if not timed:
            continue
        timed.sort(key=lambda pair: pair[1])
        latest_trade, _latest_ts = timed[-1]
        window = [(t, ts) for t, ts in timed if at - ts <= _FIVE_MINUTES_SECONDS] or timed[-1:]
        oldest_in_window, _ = window[0]
        price_usd = latest_trade.price_usd
        price_change_m5_pct = (
            (price_usd / oldest_in_window.price_usd - 1) * 100
            if oldest_in_window.price_usd > 0 else None
        )
        supply = supply_by_mint.get(mint, LAUNCHLAB_STANDARD_SUPPLY)
        quotes[mint] = MarketQuote(
            mint=mint,
            symbol=latest_trade.symbol,
            price_sol=0.0,
            liquidity_usd=liquidity_usd,
            market_cap_usd=price_usd * supply,
            pair_address="",
            pair_created_at_ms=None,
            buys_m5=sum(1 for t, _ in window if t.side == "buy"),
            sells_m5=sum(1 for t, _ in window if t.side == "sell"),
            volume_m5_usd=sum(t.amount_usd for t, _ in window),
            price_change_m5_pct=price_change_m5_pct,
            chain="solana",
            price_usd=price_usd,
        )
    return quotes
