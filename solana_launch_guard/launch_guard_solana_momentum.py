"""Rediscovers established Solana tokens regaining momentum.

pump.fun (launch_guard_ingestion.py) and LaunchLab (launch_guard_launchlab.py)
both only ever see a mint once, near its creation - neither re-scans a token
days later. This mixin closes that gap using the same DexScreener token-
profiles/boosts discovery launch_guard_multichain.py already proves works
(discover_token_profiles), scoped to chain="solana" and filtered to tokens
at least solana_momentum_min_age_days old. Ownership is deliberately never
checked here - hunter-v1's entry logic has no "already own this mint" gate
(only a global open-position cap), so a token Launch Guard, CopyFomo, or the
user already holds some of is still eligible if it shows fresh momentum.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .launch_guard_state import LaunchGuardState
from .launch_guard_support import _is_stock_token_symbol

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
            discovered = await self.oracle.discover_token_profiles("solana")
            stock_symbols = await self.oracle.robinhood_stock_token_symbols()
            semaphore = asyncio.Semaphore(5)

            async def fetch(address: str, limiter: asyncio.Semaphore = semaphore):
                async with limiter:
                    return address, await self.oracle.quote(address, chain="solana")

            try:
                quotes = await asyncio.gather(*(fetch(address) for address in discovered))
                now_ms = time.time() * 1000
                for address, quote in quotes:
                    if quote is None:
                        continue
                    # An unknown age can't be verified as >= the minimum, so
                    # it's excluded rather than assumed old enough.
                    if quote.pair_created_at_ms is None:
                        continue
                    if now_ms - quote.pair_created_at_ms < min_age_ms:
                        continue
                    if stock_symbols is not None and _is_stock_token_symbol(
                        quote.symbol, stock_symbols
                    ):
                        LOGGER.info(
                            "SOLANA MOMENTUM REJECT %-10s contract=%s "
                            "reason=tokenized stock symbol",
                            quote.symbol,
                            address,
                        )
                        continue
                    result = self.intelligence.score(quote)
                    current_result = (result.tier, result.total_score)
                    previous_result = self.solana_momentum_last_result.get(address)
                    if current_result != previous_result:
                        LOGGER.info(
                            "SOLANA MOMENTUM %-9s %-10s score=%d contract=%s %s",
                            result.tier,
                            quote.symbol,
                            result.total_score,
                            address,
                            "; ".join(result.reasons),
                        )
                        self.solana_momentum_last_result[address] = current_result

                    if not result.accepted:
                        continue
                    self.store.save_intelligence_score(
                        mint=address,
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
