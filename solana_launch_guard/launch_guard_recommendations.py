from __future__ import annotations

import asyncio
import logging
import sys
import time

from .launch_guard_state import LaunchGuardState
from .market_structure import classify_candle_pattern
from .recommendations import (
    ACTIONABLE_BUY_DECISIONS,
    build_snapshot,
    format_recommendations,
    write_snapshot,
)

LOGGER = logging.getLogger("solana_launch_guard")


class RecommendationMonitorMixin(LaunchGuardState):
    """The recommendation dashboard refresh loop."""

    def _record_buy_signals(self) -> None:
        """Log each move INTO a buy decision (not every poll it stays there),
        so the outcome tracker can measure each signal from the moment it
        fired. Evaluation only: failures are logged and never block trading."""
        current = self.recommendations.candidates
        for key in [k for k in self.buy_signal_last_decision if k not in current]:
            del self.buy_signal_last_decision[key]
        for key, candidate in current.items():
            decision = candidate.decision
            previous = self.buy_signal_last_decision.get(key)
            self.buy_signal_last_decision[key] = decision
            if decision not in ACTIONABLE_BUY_DECISIONS or decision == previous:
                continue
            try:
                signal_id = self.store.save_buy_signal(
                    mint=candidate.mint,
                    symbol=candidate.symbol,
                    chain=candidate.chain,
                    decision=decision,
                    price=candidate.current_price,
                    price_currency=candidate.price_currency,
                    liquidity_usd=candidate.liquidity_usd,
                    signal_score=candidate.signal_score,
                    pair_created_at_ms=candidate.pair_created_at_ms,
                    reason=candidate.decision_reason,
                    live_blocked_reason=self.strategy_profile.entry_block_reason(
                        decision, candidate.pair_created_at_ms
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - evaluation must not break the loop
                LOGGER.warning("Could not record %s signal for %s: %s",
                               decision, candidate.symbol, exc)
                continue
            if candidate.chain != "solana":
                continue
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # called outside the monitor loop
                self.store.tag_buy_signal(signal_id, "unavailable", None)
                continue
            task = loop.create_task(self._tag_signal_candle(
                signal_id, pool=candidate.pair_address or "", mint=candidate.mint
            ))
            self.signal_tag_tasks.add(task)
            task.add_done_callback(self.signal_tag_tasks.discard)

    async def _tag_signal_candle(self, signal_id: int, *, pool: str, mint: str) -> None:
        """Record the latest closed 1-minute candle's shape for a signal that
        just fired. Research only; never delays or blocks the monitor."""
        try:
            candles = await self.signal_candle_scanner._closed_minute_candles(
                pool=pool, mint=mint
            )
            tag = classify_candle_pattern(candles or [], now=time.time())
            self.store.tag_buy_signal(signal_id, str(tag["pattern"]), tag.get("trend"))
        except Exception as exc:  # noqa: BLE001 - evaluation must not break the loop
            LOGGER.warning("Could not tag candle for signal %s: %s", signal_id, exc)

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

            self._record_buy_signals()

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
            snapshot = build_snapshot(
                self.recommendations.ranked_for_execution(
                    self.settings.recommendation_limit
                ),
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
