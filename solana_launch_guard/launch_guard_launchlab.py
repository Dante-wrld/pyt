"""Raydium LaunchLab (stonk.fun, LetsBONK.fun, ...) discovery via Bitquery.

Mirrors launch_guard_multichain.py's run_multichain_feed: build a chain-
agnostic MarketQuote, score it with the same CoinIntelligence.score, and
feed accepted candidates into the same shared RecommendationBook - no
pump.fun-specific Launch/core.py adaptation needed. See bitquery.py's
module docstring for why this reuses that pattern instead.
"""
from __future__ import annotations

import asyncio
import logging

from .bitquery import BitqueryAuthError, build_launchlab_quotes
from .launch_guard_state import LaunchGuardState
from .launch_guard_support import hunter_v1_is_at_capacity

LOGGER = logging.getLogger("solana_launch_guard")


class LaunchLabFeedMixin(LaunchGuardState):
    """LaunchLab paper-recommendation feed (Bitquery, no automatic orders)."""

    async def run_launchlab_feed(self) -> None:
        client = self.bitquery_client
        if client is None:
            LOGGER.warning(
                "BITQUERY_CLIENT_ID/BITQUERY_CLIENT_SECRET not configured; "
                "LaunchLab discovery feed inactive"
            )
            return
        LOGGER.info(
            "LaunchLab paper-recommendation feed active (no automatic orders)"
        )
        seen_creation_signatures: set[str] = set()
        while True:
            if hunter_v1_is_at_capacity(path=self.settings.hunter_capacity_snapshot_path):
                # This feed only ever produces fresh-origin candidates, so
                # hunter-v1 having no room for a new fresh position means
                # nothing could act on a poll's result anyway - skip the
                # Bitquery calls entirely rather than spending quota on
                # them, but keep checking at a slow trickle (not a full
                # stop) so the pool isn't cold the moment a slot frees up.
                await asyncio.sleep(self.settings.launchlab_throttled_poll_seconds)
                continue
            try:
                creations, trades, pools = await asyncio.gather(
                    client.recent_pool_creations(),
                    client.recent_trades(),
                    client.recent_pools(),
                )
                # recent_pool_creations() always returns its newest-N window,
                # so a signature that scrolls out of it never comes back -
                # replacing the seen-set with this poll's own signatures
                # (rather than accumulating forever) is enough to dedup
                # consecutive polls without unbounded growth.
                for creation in creations:
                    if creation.signature not in seen_creation_signatures:
                        LOGGER.info(
                            "LAUNCHLAB NEW      %-10s mint=%s creator=%s",
                            creation.symbol,
                            creation.mint,
                            creation.creator,
                        )
                seen_creation_signatures = {c.signature for c in creations}

                quotes = build_launchlab_quotes(
                    trades=trades, pools=pools, creations=tuple(creations)
                )
                for mint, quote in quotes.items():
                    result = self.intelligence.score(quote)
                    current_result = (result.tier, result.total_score)
                    previous_result = self.launchlab_last_result.get(mint)
                    if current_result != previous_result:
                        LOGGER.info(
                            "LAUNCHLAB %-9s %-10s score=%d contract=%s %s",
                            result.tier,
                            quote.symbol,
                            result.total_score,
                            mint,
                            "; ".join(result.reasons),
                        )
                        self.launchlab_last_result[mint] = current_result

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
            except BitqueryAuthError as exc:
                LOGGER.warning("LaunchLab feed: Bitquery auth error: %s", exc)
            except (OSError, RuntimeError, ValueError) as exc:
                LOGGER.warning("LaunchLab feed: poll failed: %s", exc)

            await asyncio.sleep(self.settings.launchlab_poll_seconds)
