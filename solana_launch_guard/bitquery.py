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

Not yet wired into launch_guard_ingestion.py's discovery pipeline or
core.py's risk-scoring gate: LaunchLab's pool-creation event doesn't
carry pump.fun-equivalent fields (no live virtual reserves, no
creator-buy-at-launch signal - see decide whether/how to adapt Launch's
hard-required fields, or build a parallel eligibility path, before this
is live-trading-connected). This module only covers the confirmed,
tested surface: OAuth token refresh, recent pool creations, and recent
trades (for live price/volume tracking once a mint is known).
"""
from __future__ import annotations

import asyncio
import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import certifi

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

    async def recent_pool_creations(self, *, limit: int = 20) -> list[LaunchLabPoolCreation]:
        return await asyncio.to_thread(self._recent_pool_creations, limit)

    async def recent_trades(self, *, limit: int = 50) -> list[LaunchLabTrade]:
        return await asyncio.to_thread(self._recent_trades, limit)

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
        with urllib.request.urlopen(request, timeout=30, context=self._ssl) as response:
            payload = json.load(response)
        errors = payload.get("errors")
        if errors:
            raise RuntimeError(f"Bitquery GraphQL error: {errors}")
        return payload.get("data") or {}

    def _recent_pool_creations(self, limit: int) -> list[LaunchLabPoolCreation]:
        query = f"""
        query {{
          Solana {{
            Instructions(
              limit: {{count: {int(limit)}}}
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
              Block {{ Time }}
              Transaction {{ Signer Signature }}
              Instruction {{
                Accounts {{ Address Token {{ Mint Owner }} }}
                Program {{
                  Arguments {{
                    Name
                    Value {{
                      ... on Solana_ABI_Json_Value_Arg {{ json }}
                    }}
                  }}
                }}
              }}
            }}
          }}
        }}
        """
        data = self._graphql(query)
        results: list[LaunchLabPoolCreation] = []
        for row in data.get("Solana", {}).get("Instructions", []) or []:
            parsed = _parse_pool_creation(row)
            if parsed is not None:
                results.append(parsed)
        return results

    def _recent_trades(self, limit: int) -> list[LaunchLabTrade]:
        query = f"""
        query {{
          Solana {{
            DEXTradeByTokens(
              limit: {{count: {int(limit)}}}
              orderBy: {{descending: Block_Time}}
              where: {{
                Trade: {{Dex: {{ProtocolName: {{is: "{LAUNCHLAB_PROTOCOL_NAME}"}}}}}}
              }}
            ) {{
              Block {{ Time }}
              Trade {{
                Currency {{ MintAddress Symbol }}
                PriceInUSD
                Side {{ Type AmountInUSD }}
              }}
            }}
          }}
        }}
        """
        data = self._graphql(query)
        results: list[LaunchLabTrade] = []
        for row in data.get("Solana", {}).get("DEXTradeByTokens", []) or []:
            parsed = _parse_trade(row)
            if parsed is not None:
                results.append(parsed)
        return results


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
