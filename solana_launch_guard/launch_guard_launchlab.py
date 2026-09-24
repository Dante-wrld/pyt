"""Raydium LaunchLab (stonk.fun, LetsBONK.fun, ...) discovery via Bitquery.

Mirrors launch_guard_multichain.py's run_multichain_feed: build a chain-
agnostic MarketQuote, score it with the same CoinIntelligence.score, and
feed accepted candidates into the same shared RecommendationBook - no
pump.fun-specific Launch/core.py adaptation needed. See bitquery.py's
module docstring for why this reuses that pattern instead.

Runs a discover-then-confirm funnel rather than scoring every poll's raw
results immediately: a broad discovery pass finds newly active mints and
holds them in a pending set; only after launchlab_trim_window_seconds does
a separate, targeted re-check (filtered to just those mints, not another
broad window) decide whether each one is promoted into the shared
RecommendationBook or dropped. A mint is promoted only if it BOTH still
scores CORE/MOONSHOT after the wait AND has moved at least
launchlab_trim_min_price_move_pct in price from its discovery-time
baseline - score alone can pass a token that simply sat still, movement
alone can pass one that's still objectively unsafe. Matching the trim
window to the discovery poll interval keeps this to at most two Bitquery
calls per tick (discovery, plus a trim-check only when something is due)
instead of scoring - and paying for - every poll's results unconditionally.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .bitquery import BitqueryAuthError, BitqueryClient, build_launchlab_quotes
from .launch_guard_state import LaunchGuardState
from .launch_guard_support import hunter_v1_is_at_capacity
from .market import MarketQuote

LOGGER = logging.getLogger("solana_launch_guard")

# A defensive cap on the pending set, in case trim-checks stop draining it
# (e.g. a prolonged Bitquery outage) - self-heals once polling resumes,
# this just bounds memory growth in the meantime rather than relying on
# that recovery happening quickly.
_MAX_PENDING = 200


def price_move_pct(baseline_price: float | None, fresh_price: float | None) -> float:
    """Absolute percent change from a discovery-time baseline price to a
    trim-check's fresh price. 0.0 (never "moved enough") whenever either
    price is missing or non-positive, rather than raising or guessing -
    matches every other quote field's missing-data handling in this feed."""
    if not baseline_price or baseline_price <= 0 or not fresh_price:
        return 0.0
    return abs(fresh_price - baseline_price) / baseline_price * 100


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
            "LaunchLab discover-then-confirm feed active (no automatic orders)"
        )
        seen_creation_signatures: set[str] = set()
        pending: dict[str, tuple[float, MarketQuote]] = {}
        while True:
            if hunter_v1_is_at_capacity(path=self.settings.hunter_capacity_snapshot_path):
                # This feed only ever produces fresh-origin candidates, so
                # hunter-v1 having no room for a new fresh position means
                # nothing could act on a poll's result anyway - skip both
                # phases entirely rather than spending quota on them, but
                # keep checking at a slow trickle (not a full stop) so the
                # pool isn't cold the moment a slot frees up. Pending mints
                # simply wait longer for their trim-check; nothing is lost.
                await asyncio.sleep(self.settings.launchlab_throttled_poll_seconds)
                continue

            now = time.time()
            due_mints = [
                mint for mint, (discovered_at, _baseline) in pending.items()
                if now - discovered_at >= self.settings.launchlab_trim_window_seconds
            ]
            if due_mints:
                await self._trim_check(client, pending, due_mints)

            try:
                creations, trades, pools = await client.recent_launchlab_snapshot()
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
                    if mint in pending or quote.recommendation_key in self.recommendations.candidates:
                        continue
                    if len(pending) >= _MAX_PENDING:
                        break
                    pending[mint] = (now, quote)
            except BitqueryAuthError as exc:
                LOGGER.warning("LaunchLab feed: Bitquery auth error: %s", exc)
            except (OSError, RuntimeError, ValueError) as exc:
                LOGGER.warning("LaunchLab feed: poll failed: %s", exc)

            await asyncio.sleep(self.settings.launchlab_poll_seconds)

    async def _trim_check(
        self, client: BitqueryClient,
        pending: dict[str, tuple[float, MarketQuote]], due_mints: list[str],
    ) -> None:
        try:
            trades, pools = await client.launchlab_activity_for_mints(due_mints)
            fresh_quotes = build_launchlab_quotes(trades=trades, pools=pools, creations=())
        except BitqueryAuthError as exc:
            LOGGER.warning("LaunchLab trim-check: Bitquery auth error: %s", exc)
            return
        except (OSError, RuntimeError, ValueError) as exc:
            LOGGER.warning("LaunchLab trim-check: poll failed: %s", exc)
            return
        for mint in due_mints:
            _discovered_at, baseline = pending.pop(mint)
            fresh = fresh_quotes.get(mint)
            if fresh is None:
                LOGGER.info(
                    "LAUNCHLAB TRIM DROP  %-10s mint=%s no recent activity since discovery",
                    baseline.symbol,
                    mint,
                )
                continue
            result = self.intelligence.score(fresh)
            move_pct = price_move_pct(baseline.price_usd, fresh.price_usd)
            moved_enough = move_pct >= self.settings.launchlab_trim_min_price_move_pct
            if not result.accepted or not moved_enough:
                LOGGER.info(
                    "LAUNCHLAB TRIM DROP  %-9s %-10s score=%d move=%.1f%% contract=%s",
                    result.tier,
                    fresh.symbol,
                    result.total_score,
                    move_pct,
                    mint,
                )
                continue
            LOGGER.info(
                "LAUNCHLAB TRIM KEEP  %-9s %-10s score=%d move=%.1f%% contract=%s %s",
                result.tier,
                fresh.symbol,
                result.total_score,
                move_pct,
                mint,
                "; ".join(result.reasons),
            )
            self.store.save_intelligence_score(
                mint=mint,
                symbol=fresh.symbol,
                tier=result.tier,
                total_score=result.total_score,
                safety_score=result.safety_score,
                momentum_score=result.momentum_score,
                reasons=result.reasons,
            )
            self.recommendations.add(fresh, result)
