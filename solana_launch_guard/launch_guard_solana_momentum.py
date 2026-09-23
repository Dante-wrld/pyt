"""Rediscovers established Solana tokens regaining momentum.

pump.fun (launch_guard_ingestion.py) and LaunchLab (launch_guard_launchlab.py)
both only ever see a mint once, near its creation - neither re-scans a token
days later. This mixin closes that gap using GeckoTerminal's trending_pools
(see geckoterminal.py's docstring for why DexScreener's token-profiles feed,
tried first, was swapped out), filtered to tokens at least
solana_momentum_min_age_days old. Ownership is deliberately never checked
here - hunter-v1's entry logic has no "already own this mint" gate (only a
global open-position cap), so a token Launch Guard, CopyFomo, or the user
already holds some of is still eligible if it shows fresh momentum.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .geckoterminal import build_gecko_quotes
from .launch_guard_state import LaunchGuardState
from .launch_guard_support import _is_stock_token_symbol, hunter_v1_is_at_capacity

LOGGER = logging.getLogger("solana_launch_guard")


class SolanaMomentumFeedMixin(LaunchGuardState):
    """Paper-recommendation feed for aged, momentum-showing Solana tokens."""

    async def run_solana_momentum_feed(self) -> None:
        LOGGER.info(
            "Solana momentum rediscovery feed active (age >= %.0fd, "
            "no automatic orders)",
            self.settings.solana_momentum_min_age_days,
        )
        min_age_ms = self.settings.solana_momentum_min_age_days * 86_400_000
        while True:
            if hunter_v1_is_at_capacity(path=self.settings.hunter_capacity_snapshot_path):
                # This feed only ever produces fresh-origin candidates, so
                # hunter-v1 having no room for a new fresh position means
                # nothing could act on a poll's result anyway - skip the
                # GeckoTerminal call entirely rather than spending quota on
                # it, but keep checking at a slow trickle (not a full stop)
                # so the pool isn't cold the moment a slot frees up.
                await asyncio.sleep(self.settings.solana_momentum_throttled_poll_seconds)
                continue
            try:
                pools = await self.gecko_client.trending_pools()
                quotes = build_gecko_quotes(pools)
                stock_symbols = await self.oracle.robinhood_stock_token_symbols()
                now_ms = time.time() * 1000
                for mint, quote in quotes.items():
                    assert quote.pair_created_at_ms is not None  # set by build_gecko_quotes
                    if now_ms - quote.pair_created_at_ms < min_age_ms:
                        continue
                    if stock_symbols is not None and _is_stock_token_symbol(
                        quote.symbol, stock_symbols
                    ):
                        LOGGER.info(
                            "SOLANA MOMENTUM REJECT %-10s contract=%s "
                            "reason=tokenized stock symbol",
                            quote.symbol,
                            mint,
                        )
                        continue
                    result = self.intelligence.score(quote)
                    current_result = (result.tier, result.total_score)
                    previous_result = self.solana_momentum_last_result.get(mint)
                    if current_result != previous_result:
                        LOGGER.info(
                            "SOLANA MOMENTUM %-9s %-10s score=%d contract=%s %s",
                            result.tier,
                            quote.symbol,
                            result.total_score,
                            mint,
                            "; ".join(result.reasons),
                        )
                        self.solana_momentum_last_result[mint] = current_result

                    if not result.accepted:
                        continue
                    self.store.save_intelligence_score(
                        mint=mint,
                        symbol=quote.symbol,
                        tier=result.tier,
                        total_score=result.total_score,
                        safety_score=result.safety_score,
                        momentum_score=result.momentum_score,
                        reasons=result.reasons,
                    )
                    self.recommendations.add(quote, result)
            except (OSError, RuntimeError, ValueError) as exc:
                LOGGER.warning("Solana momentum feed: poll failed: %s", exc)

            await asyncio.sleep(self.settings.solana_momentum_poll_seconds)
