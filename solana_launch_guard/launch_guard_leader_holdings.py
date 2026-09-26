"""Leader-held tokens as a watchlist: your strategy decides, not their trades.

Copying a leader's trade means buying after them, so the copy is always
late. This feed instead reads what the CopyFomo leaders
(COPYFOMO_LEADER_WALLETS) currently HOLD and puts those tokens on the
recommendation board, where the same entry rules as every other feed decide
if and when to buy. Nobody waits for a leader to trade.

Holding is a discovery source, not a signal: a leader may be stuck in a
losing bag. So only holdings worth at least LEADER_HOLDINGS_MIN_USD, in pools
with at least LEADER_HOLDINGS_MIN_LIQUIDITY_USD of liquidity, are
considered, tokenized stocks are skipped, and at most
LEADER_HOLDINGS_MAX_CANDIDATES (largest combined leader value first) are
added per poll, so leader tokens cannot flood the 30-slot board.

Tokens are tagged source="leader-held". By default that source is in
ENTRY_SHADOW_ONLY_SOURCES, so a token only this feed found is never bought
live; it is still scored, signalled, candle-tagged and tracked, and
`launch-guard-eval report --group signal:` splits results by source.

The latest holdings are also cached per leader so a leader's sell can be
sized against what they held (see copyfomo_monitor's exit warning).
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .config import _float, _int
from .launch_guard_state import LaunchGuardState
from .launch_guard_support import _is_stock_token_symbol
from .market import MarketQuote
from .wallet import SolanaRpc, SolanaTokenHolding

LOGGER = logging.getLogger("solana_launch_guard")

SOURCE = "leader-held"


@dataclass(frozen=True, slots=True)
class LeaderHoldingsConfig:
    poll_seconds: float = 600.0
    min_holding_usd: float = 100.0
    min_liquidity_usd: float = 50_000.0
    max_candidates: int = 10

    @classmethod
    def from_env(cls) -> LeaderHoldingsConfig:
        return cls(
            poll_seconds=max(60.0, _float("LEADER_HOLDINGS_POLL_SECONDS", 600.0)),
            min_holding_usd=_float("LEADER_HOLDINGS_MIN_USD", 100.0),
            min_liquidity_usd=_float(
                "LEADER_HOLDINGS_MIN_LIQUIDITY_USD",
                float(os.getenv("AUTO_BUY_DISCOVERY_MIN_LIQUIDITY_USD") or 50_000),
            ),
            max_candidates=max(1, _int("LEADER_HOLDINGS_MAX_CANDIDATES", 10)),
        )


@dataclass(frozen=True, slots=True)
class LeaderHolding:
    mint: str
    symbol: str
    leaders: tuple[str, ...]
    value_usd: float
    quote: MarketQuote


def select_leader_holdings(
    holdings: Mapping[str, Iterable[SolanaTokenHolding]],
    quotes: Mapping[str, MarketQuote | None],
    config: LeaderHoldingsConfig,
    *,
    stock_symbols: frozenset[str] | None = None,
) -> list[LeaderHolding]:
    """Leader holdings worth watching, largest combined value first."""
    value: dict[str, float] = {}
    held_by: dict[str, list[str]] = {}
    for leader, items in holdings.items():
        for item in items:
            quote = quotes.get(item.mint)
            price = quote.price_usd if quote else None
            if not price or price <= 0:
                continue
            usd = item.amount * price
            if usd < config.min_holding_usd:
                continue  # dust or airdropped spam
            value[item.mint] = value.get(item.mint, 0.0) + usd
            held_by.setdefault(item.mint, []).append(leader)
    selected = []
    for mint, usd in value.items():
        quote = quotes[mint]
        assert quote is not None
        if (quote.liquidity_usd or 0.0) < config.min_liquidity_usd:
            continue
        if stock_symbols is not None and _is_stock_token_symbol(
            quote.symbol, stock_symbols
        ):
            continue
        selected.append(LeaderHolding(
            mint, quote.symbol, tuple(sorted(held_by[mint])), usd, quote,
        ))
    selected.sort(key=lambda h: h.value_usd, reverse=True)
    return selected[: config.max_candidates]


class LeaderHoldingsFeedMixin(LaunchGuardState):
    """Adds tokens the CopyFomo leaders hold to the board (shadow source)."""

    async def run_leader_holdings_feed(self) -> None:
        leaders = self.settings.copyfomo_leader_wallets
        config = LeaderHoldingsConfig.from_env()
        LOGGER.info(
            "Leader-held feed active: %d leaders, poll %.0fs, min $%.0f held, "
            "min $%.0f liquidity, up to %d tokens (source=%s)",
            len(leaders), config.poll_seconds, config.min_holding_usd,
            config.min_liquidity_usd, config.max_candidates, SOURCE,
        )
        rpc = SolanaRpc(self.settings.solana_rpc_http_url)
        while True:
            try:
                await self.poll_leader_holdings(rpc, leaders, config)
            except (OSError, RuntimeError, ValueError) as exc:
                LOGGER.warning("Leader-held feed: poll failed: %s", exc)
            await asyncio.sleep(config.poll_seconds)

    async def poll_leader_holdings(
        self,
        rpc: SolanaRpc,
        leaders: Iterable[tuple[str, str]],
        config: LeaderHoldingsConfig,
    ) -> list[LeaderHolding]:
        holdings: dict[str, tuple[SolanaTokenHolding, ...]] = {}
        for name, address in leaders:
            try:
                holdings[name] = await rpc.token_holdings(address)
            except ConnectionError as exc:
                LOGGER.warning(
                    "Leader-held feed: %s holdings unavailable: %s", name, exc
                )
                continue
            # Cache amounts so a later sell can be sized against the position.
            for mint in [m for m, by in self.leader_holdings.items() if name in by]:
                self.leader_holdings[mint].pop(name, None)
            for item in holdings[name]:
                self.leader_holdings.setdefault(item.mint, {})[name] = item.amount
            await asyncio.sleep(0.5)  # be gentle with the public RPC
        mints = sorted({item.mint for items in holdings.values() for item in items})
        if not mints:
            return []
        quotes = await self.oracle.quote_many(mints, chain="solana")
        stock_symbols = await self.oracle.robinhood_stock_token_symbols()
        selected = select_leader_holdings(
            holdings, quotes, config, stock_symbols=stock_symbols
        )
        for holding in selected:
            result = self.intelligence.score(holding.quote)
            if not result.accepted:
                continue
            candidate = self.recommendations.add(holding.quote, result, source=SOURCE)
            if candidate is not None:
                LOGGER.info(
                    "LEADER-HELD %-10s held by %s ($%.0f) score=%d mint=%s",
                    holding.symbol, ",".join(holding.leaders), holding.value_usd,
                    result.total_score, holding.mint,
                )
        return selected
