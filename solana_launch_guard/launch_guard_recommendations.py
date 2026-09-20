from __future__ import annotations

import logging
import asyncio
import sys
import time

from .recommendations import (
    build_snapshot,
    format_recommendations,
    write_snapshot,
)

LOGGER = logging.getLogger("solana_launch_guard")


class RecommendationMonitorMixin:
    """The recommendation dashboard refresh loop."""

    async def run_recommendation_monitor(self) -> None:
        LOGGER.info(
            "Recommendation monitor active (top %d, refresh %.0fs)",
            self.settings.recommendation_limit,
            self.settings.recommendation_poll_seconds,
        )
        while True:
            self.recommendations.expire()
            candidates = list(self.recommendations.candidates.values())

            semaphore = asyncio.Semaphore(5)

            async def refresh(
                mint: str,
                chain: str,
                limiter: asyncio.Semaphore = semaphore,
            ) -> None:
                async with limiter:
                    quote = await self.oracle.quote(mint, chain=chain)
                if quote is not None:
                    candidate = self.recommendations.update(quote)
                    if (
                        candidate is not None
                        and chain == "solana"
                        and self.settings.auto_buy_enabled
                        and self.settings.auto_buy_discovery
                    ):
                        self.store.save_auto_buy_discovery_candidate(
                            token_address=candidate.mint,
                            symbol=candidate.symbol,
                            candidate_json=candidate.to_json(),
                            tier=candidate.tier,
                            intelligence_score=(
                                candidate.intelligence_score
                            ),
                            signal_score=candidate.signal_score,
                            decision=candidate.decision,
                            liquidity_usd=candidate.liquidity_usd,
                            next_check_epoch=(
                                time.time()
                                + self.settings.auto_buy_watch_retry_max_seconds
                            ),
                            reason=candidate.decision_reason,
                        )

            if candidates:
                await asyncio.gather(
                    *(
                        refresh(candidate.mint, candidate.chain)
                        for candidate in candidates
                    )
                )
                ranked = self.recommendations.ranked(
                    self.settings.recommendation_limit
                )
                if ranked and self.recommendation_console_output:
                    use_color = self.settings.color_output and sys.stderr.isatty()
                    LOGGER.info("\n%s", format_recommendations(ranked, color=use_color))

            if self.notifier is not None:
                notification_candidates = list(
                    self.recommendations.candidates.values()
                )
                for candidate in notification_candidates:
                    try:
                        sent = await self.notifier.maybe_send(candidate)
                    except ConnectionError as exc:
                        LOGGER.warning(
                            "Phone notification unavailable for %s (%s)",
                            candidate.symbol,
                            exc,
                        )
                        continue
                    if sent:
                        LOGGER.info(
                            "PHONE ALERT %s chain=%s decision=%s score=%d",
                            candidate.symbol,
                            candidate.chain,
                            candidate.decision,
                            candidate.signal_score,
                        )

            if self.settings.auto_buy_enabled:
                for candidate in list(
                    self.recommendations.candidates.values()
                ):
                    await self._maybe_auto_buy(candidate)

            try:
                self.pullback_tracker.record(self.recommendations)
            except OSError as exc:
                LOGGER.warning("Could not persist pullback tracking: %s", exc)

            alerts = (
                self.recommendations.pop_pullback_alerts()
                + self.recommendations.pop_buy_zone_alerts()
            )
            for candidate in alerts:
                price_prefix = "$" if candidate.price_currency == "USD" else ""
                LOGGER.info(
                    "%s ALERT %s chain=%s current=%s%.12g "
                    "entry=%s%.12g-%s%.12g",
                    candidate.decision,
                    candidate.symbol,
                    candidate.chain,
                    price_prefix,
                    candidate.current_price,
                    price_prefix,
                    candidate.entry_zone_low or 0,
                    price_prefix,
                    candidate.entry_zone_high or 0,
                )
            ranked = self.recommendations.ranked(
                self.settings.recommendation_limit
            )
            snapshot = build_snapshot(
                ranked,
                tracked_candidates=list(self.recommendations.candidates.values()),
                pending_count=(
                    len(self.candidate_tasks) + self.multichain_pending_count
                ),
                poll_seconds=self.settings.recommendation_poll_seconds,
                alerts=alerts,
            )
            try:
                write_snapshot(
                    self.settings.recommendation_snapshot_path, snapshot
                )
            except OSError as exc:
                LOGGER.warning("Could not update recommendation window: %s", exc)

            await asyncio.sleep(self.settings.recommendation_poll_seconds)
