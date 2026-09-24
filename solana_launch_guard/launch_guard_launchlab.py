"""Raydium LaunchLab (stonk.fun, LetsBONK.fun, ...) discovery via Bitquery.

Mirrors launch_guard_multichain.py's run_multichain_feed: build a chain-
agnostic MarketQuote, score it with the same CoinIntelligence.score, and
feed accepted candidates into the same shared RecommendationBook - no
pump.fun-specific Launch/core.py adaptation needed. See bitquery.py's
module docstring for why this reuses that pattern instead.

Runs a discover-then-confirm funnel rather than scoring every poll's raw
results immediately: a broad discovery pass finds newly active mints and
holds them in a pending set; only once each one is due does a separate,
targeted re-check (filtered to just those mints, not another broad window)
decide whether it's promoted into the shared RecommendationBook, dropped,
or given one more look. A mint is promoted only if it BOTH still scores
CORE/MOONSHOT AND has moved at least launchlab_trim_min_price_move_pct in
price from its discovery-time baseline - score alone can pass a token that
simply sat still, movement alone can pass one that's still objectively
unsafe.

The loop's own wake cadence (launchlab_check_interval_seconds, default 60s)
is deliberately much finer than the broad discovery cadence
(launchlab_poll_seconds, default 600s): checking whether anything in the
pending set is due is a pure time comparison, free regardless of how often
it runs, so decoupling the two lets a trim-check fire close to exactly when
it's due instead of waiting for whichever discovery tick happens to land
after it. The expensive, unconditional call - broad discovery - still only
runs on its own slower cadence, so this costs nothing extra by itself.

Two refinements sit on top of the base funnel, both bounded so they can't
reopen the cost problem launchlab_poll_seconds was widened to fix:
- A mint whose discovery-time baseline already looks unusually active
  (launchlab_hot_min_trades) gets a shorter dwell
  (launchlab_hot_trim_window_seconds) before its first trim-check instead
  of the full window every other discovery waits out.
- A mint that still scores CORE/MOONSHOT but simply hasn't moved enough
  yet gets exactly one more look, launchlab_trim_retry_window_seconds
  later, instead of being dropped the instant its window elapses. A mint
  that fails the safety/score bar gets no retry - more time doesn't fix a
  bad token, so there's nothing to wait for there.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace

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


@dataclass(slots=True)
class _Pending:
    baseline: MarketQuote
    due_at: float
    retries_used: int = 0


def price_move_pct(baseline_price: float | None, fresh_price: float | None) -> float:
    """Absolute percent change from a discovery-time baseline price to a
    trim-check's fresh price. 0.0 (never "moved enough") whenever either
    price is missing or non-positive, rather than raising or guessing -
    matches every other quote field's missing-data handling in this feed."""
    if not baseline_price or baseline_price <= 0 or not fresh_price:
        return 0.0
    return abs(fresh_price - baseline_price) / baseline_price * 100


def is_hot_baseline(quote: MarketQuote, *, min_trades: int) -> bool:
    """A discovery-time baseline whose combined 5-minute buys+sells already
    clears this bar looks unusually active - it earns a shorter dwell
    before its first trim-check instead of the standard window every other
    discovery waits out."""
    return (quote.buys_m5 + quote.sells_m5) >= min_trades


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
        pending: dict[str, _Pending] = {}
        last_discovery_at = 0.0
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
            due_mints = [mint for mint, entry in pending.items() if now >= entry.due_at]
            if due_mints:
                await self._trim_check(client, pending, due_mints)

            if now - last_discovery_at >= self.settings.launchlab_poll_seconds:
                last_discovery_at = now
                try:
                    creations, trades, pools = await client.recent_launchlab_snapshot()
                    # recent_pool_creations() always returns its newest-N
                    # window, so a signature that scrolls out of it never
                    # comes back - replacing the seen-set with this poll's
                    # own signatures (rather than accumulating forever) is
                    # enough to dedup consecutive polls without unbounded
                    # growth.
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
                        hot = is_hot_baseline(quote, min_trades=self.settings.launchlab_hot_min_trades)
                        window = (
                            self.settings.launchlab_hot_trim_window_seconds if hot
                            else self.settings.launchlab_trim_window_seconds
                        )
                        pending[mint] = _Pending(baseline=quote, due_at=now + window)
                except BitqueryAuthError as exc:
                    LOGGER.warning("LaunchLab feed: Bitquery auth error: %s", exc)
                except (OSError, RuntimeError, ValueError) as exc:
                    LOGGER.warning("LaunchLab feed: poll failed: %s", exc)

            await asyncio.sleep(self.settings.launchlab_check_interval_seconds)

    async def _trim_check(
        self, client: BitqueryClient,
        pending: dict[str, _Pending], due_mints: list[str],
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
            entry = pending.pop(mint)
            baseline = entry.baseline
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
            if result.accepted and not moved_enough and entry.retries_used < 1:
                # Still a safe, quality candidate - just hasn't shown its
                # move yet. One extra look, not an outright drop.
                pending[mint] = replace(
                    entry,
                    due_at=time.time() + self.settings.launchlab_trim_retry_window_seconds,
                    retries_used=entry.retries_used + 1,
                )
                LOGGER.info(
                    "LAUNCHLAB TRIM WAIT  %-9s %-10s score=%d move=%.1f%% contract=%s "
                    "retrying in %.0fs",
                    result.tier,
                    fresh.symbol,
                    result.total_score,
                    move_pct,
                    mint,
                    self.settings.launchlab_trim_retry_window_seconds,
                )
                continue
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
