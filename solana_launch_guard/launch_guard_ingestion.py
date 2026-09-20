from __future__ import annotations

import logging
import asyncio
import certifi
import json
import ssl
import time
import websockets
from collections.abc import Mapping
from typing import Any

from .core import Launch
from .intelligence import IntelligenceResult
from .market import MarketQuote
from .recommendations import RecommendationCandidate

LOGGER = logging.getLogger("solana_launch_guard")


class LaunchIngestionMixin:
    """New-launch discovery, intelligence scoring, and the auto-buy discovery watch loop."""

    async def run_launch_feed(self) -> None:
        backoff_seconds = 1
        tls_context = ssl.create_default_context(cafile=certifi.where())
        while True:
            try:
                LOGGER.info("Connecting to the new-token feed")
                async with websockets.connect(
                    self.settings.websocket_uri,
                    ssl=tls_context,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                    max_size=2_000_000,
                ) as websocket:
                    await websocket.send(
                        json.dumps({"method": "subscribeNewToken"})
                    )
                    LOGGER.info("Subscribed to new token creations")
                    backoff_seconds = 1

                    async for raw_message in websocket:
                        try:
                            payload = json.loads(raw_message)
                        except (json.JSONDecodeError, TypeError):
                            LOGGER.warning("Ignoring non-JSON feed message")
                            continue
                        if not isinstance(payload, dict):
                            continue
                        if str(payload.get("txType") or "").lower() == "create":
                            await self.handle_launch(payload)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning(
                    "Launch feed disconnected (%s). Reconnecting in %d seconds",
                    exc,
                    backoff_seconds,
                )
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 30)

    async def handle_launch(self, payload: Mapping[str, Any]) -> None:
        try:
            launch = Launch.from_payload(payload)
        except ValueError as exc:
            LOGGER.warning("Ignoring malformed creation event: %s", exc)
            self.store.save_event("MALFORMED_CREATE", payload)
            return

        self.store.save_event("CREATE", payload, launch.mint)
        decision = self.risk.evaluate_candidate(launch)
        self.store.save_decision(launch, decision)

        if not decision.accepted:
            LOGGER.info(
                "REJECT %-10s score=%d mint=%s reasons=%s",
                launch.symbol,
                decision.score,
                launch.mint,
                "; ".join(decision.reasons),
            )
            return

        if (
            self.settings.auto_buy_enabled
            and self.settings.auto_buy_discovery
        ):
            observed = time.time()
            outcome = self.store.start_auto_buy_discovery_watch(
                token_address=launch.mint,
                symbol=launch.symbol,
                launch_payload=launch.raw,
                first_seen_epoch=observed,
                first_check_epoch=(
                    observed + self.settings.intelligence_wait_seconds
                ),
                expires_at_epoch=(
                    observed + self.settings.auto_buy_watch_max_seconds
                ),
                max_active=self.settings.auto_buy_watch_max_candidates,
            )
            if outcome == "CREATED":
                LOGGER.info(
                    "AUTO-BUY WATCH %-10s mint=%s first-check=%.0fs "
                    "lifetime=%.0fs",
                    launch.symbol,
                    launch.mint,
                    self.settings.intelligence_wait_seconds,
                    self.settings.auto_buy_watch_max_seconds,
                )
            return

        if len(self.candidate_tasks) >= self.settings.max_pending_candidates:
            LOGGER.info(
                "REJECT %-10s mint=%s reason=candidate queue full",
                launch.symbol,
                launch.mint,
            )
            return

        task = asyncio.create_task(self.evaluate_candidate(launch))
        self.candidate_tasks.add(task)
        task.add_done_callback(self.candidate_tasks.discard)
        LOGGER.info(
            "CANDIDATE %-10s mint=%s observation=%ss",
            launch.symbol,
            launch.mint,
            self.settings.intelligence_wait_seconds,
        )

    async def evaluate_candidate(self, launch: Launch) -> None:
        await asyncio.sleep(self.settings.intelligence_wait_seconds)

        quote = None
        for attempt in range(3):
            quote = await self.oracle.quote(launch.mint)
            if quote is not None:
                break
            if attempt < 2:
                await asyncio.sleep(10)

        result = self.intelligence.score(quote)
        symbol = quote.symbol if quote else launch.symbol
        self.store.save_intelligence_score(
            mint=launch.mint,
            symbol=symbol,
            tier=result.tier,
            total_score=result.total_score,
            safety_score=result.safety_score,
            momentum_score=result.momentum_score,
            reasons=result.reasons,
        )
        LOGGER.info(
            "INTELLIGENCE %-10s mint=%s tier=%s total=%d safety=%d "
            "momentum=%d %s",
            symbol,
            launch.mint,
            result.tier,
            result.total_score,
            result.safety_score,
            result.momentum_score,
            "; ".join(result.reasons),
        )
        if not result.accepted or quote is None:
            return

        self.recommendations.add(quote, result)

        await self._maybe_open_paper_position(launch, quote, result)

    async def _maybe_open_paper_position(
        self,
        launch: Launch,
        quote: MarketQuote,
        result: IntelligenceResult,
    ) -> None:
        """Preserve the existing paper-trade path for qualified watches."""

        symbol = quote.symbol
        sol_usd = await self.oracle.sol_usd_price()
        if sol_usd is None:
            LOGGER.info(
                "INTELLIGENCE REJECT %s mint=%s "
                "reason=SOL/USD price unavailable",
                symbol,
                launch.mint,
            )
            return

        size_usd = (
            self.settings.standard_trade_size_usd
            if result.tier == "CORE"
            else self.settings.moonshot_trade_size_usd
        )
        cost_sol = size_usd / sol_usd
        exposure_usd = self.broker.exposure_sol * sol_usd

        rejection = None
        if self.broker.has_position(launch.mint):
            rejection = "position already exists"
        elif self.broker.open_count >= self.settings.max_open_positions:
            rejection = "maximum open positions reached"
        elif exposure_usd + size_usd > self.settings.max_total_exposure_usd:
            rejection = "maximum USD exposure would be exceeded"

        if rejection:
            LOGGER.info(
                "INTELLIGENCE REJECT %s mint=%s tier=%s reason=%s",
                symbol,
                launch.mint,
                result.tier,
                rejection,
            )
            return

        scored_launch = Launch(
            mint=launch.mint,
            name=launch.name,
            symbol=symbol,
            creator=launch.creator,
            signature=launch.signature,
            virtual_sol=launch.virtual_sol,
            virtual_tokens=launch.virtual_tokens,
            market_cap_sol=launch.market_cap_sol,
            creator_buy_sol=launch.creator_buy_sol,
            price_sol=quote.price_sol,
            received_at=launch.received_at,
            raw=launch.raw,
        )
        if result.tier == "MOONSHOT":
            take_profit = self.settings.moonshot_take_profit_pct
            stop_loss = self.settings.moonshot_stop_loss_pct
        else:
            take_profit = self.settings.take_profit_pct
            stop_loss = self.settings.stop_loss_pct

        position = self.broker.open(
            scored_launch,
            reason=f"INTELLIGENCE:{result.tier}:{result.total_score}",
            cost_sol=cost_sol,
            take_profit_pct=take_profit,
            stop_loss_pct=stop_loss,
        )
        self.strategy.register_open(position, quote, result.tier)
        LOGGER.info(
            "INTELLIGENT PAPER BUY %-10s mint=%s tier=%s score=%d "
            "size=$%.2f cost=%.6f SOL entry=%.12g TP=+%.0f%% SL=-%.0f%%",
            position.symbol,
            position.mint,
            result.tier,
            result.total_score,
            size_usd,
            position.cost_sol,
            position.entry_price_sol,
            take_profit,
            stop_loss,
        )

    async def run_auto_buy_discovery_monitor(self) -> None:
        LOGGER.info(
            "Persistent auto-buy discovery active (lifetime %.0fs, "
            "capacity %d, batch %d)",
            self.settings.auto_buy_watch_max_seconds,
            self.settings.auto_buy_watch_max_candidates,
            self.settings.auto_buy_watch_batch_size,
        )
        while True:
            due = self.store.due_auto_buy_discovery_watches(
                limit=self.settings.auto_buy_watch_batch_size
            )
            semaphore = asyncio.Semaphore(5)

            async def evaluate(
                watch: Mapping[str, Any],
                limiter: asyncio.Semaphore = semaphore,
            ) -> None:
                async with limiter:
                    try:
                        await self._evaluate_auto_buy_discovery_watch(watch)
                    except (ConnectionError, RuntimeError, ValueError) as exc:
                        attempts = int(watch["attempts"]) + 1
                        self.store.record_auto_buy_discovery_observation(
                            token_address=str(watch["token_address"]),
                            symbol=str(watch["symbol"]),
                            status=str(watch["status"]),
                            next_check_epoch=(
                                time.time()
                                + self._auto_buy_watch_retry_delay(attempts)
                            ),
                            reason=f"temporary evaluation failure: {exc}",
                            quote_available=False,
                            candidate_json=watch.get("candidate_json"),
                            tier=watch.get("tier"),
                            intelligence_score=int(
                                watch.get("intelligence_score") or 0
                            ),
                            signal_score=int(watch.get("signal_score") or 0),
                            decision=watch.get("decision"),
                            liquidity_usd=watch.get("liquidity_usd"),
                        )
                        LOGGER.warning(
                            "AUTO-BUY WATCH RETRY %s mint=%s (%s)",
                            watch["symbol"],
                            watch["token_address"],
                            exc,
                        )

            if due:
                await asyncio.gather(*(evaluate(watch) for watch in due))
            await asyncio.sleep(
                self.settings.auto_buy_watch_retry_base_seconds
            )

    async def _evaluate_auto_buy_discovery_watch(
        self, watch: Mapping[str, Any]
    ) -> None:
        observed = time.time()
        mint = str(watch["token_address"])
        attempts = int(watch["attempts"]) + 1
        delay = self._auto_buy_watch_retry_delay(attempts)
        try:
            payload = json.loads(str(watch["launch_json"]))
            if not isinstance(payload, dict):
                raise TypeError("launch snapshot is not a JSON object")
            launch = Launch.from_payload(payload)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self.store.mark_auto_buy_discovery_watch(
                mint,
                status="REVIEW",
                reason=f"invalid persisted launch state: {exc}",
            )
            return

        quote = await self.oracle.quote(mint)
        current = self.store.load_auto_buy_discovery_watch(mint)
        if current is None or current["status"] not in {
            "WATCHING",
            "TRACKING",
            "QUALIFIED",
        }:
            return
        result = self.intelligence.score(quote)
        symbol = quote.symbol if quote is not None else launch.symbol
        self.store.save_intelligence_score(
            mint=mint,
            symbol=symbol,
            tier=result.tier,
            total_score=result.total_score,
            safety_score=result.safety_score,
            momentum_score=result.momentum_score,
            reasons=result.reasons,
        )
        if quote is None or not result.accepted:
            reason = "; ".join(result.reasons) or "market quote unavailable"
            self.store.record_auto_buy_discovery_observation(
                token_address=mint,
                symbol=symbol,
                status="WATCHING",
                next_check_epoch=observed + delay,
                reason=reason,
                quote_available=quote is not None,
                tier=result.tier,
                intelligence_score=result.total_score,
                liquidity_usd=(quote.liquidity_usd if quote else None),
                observed_epoch=observed,
            )
            return

        key = quote.recommendation_key
        candidate = self.recommendations.candidates.get(key)
        if candidate is None and watch.get("candidate_json"):
            try:
                restored = RecommendationCandidate.from_json(
                    str(watch["candidate_json"])
                )
                if restored.mint == mint and restored.chain == "solana":
                    self.recommendations.restore(restored)
            except (TypeError, ValueError, json.JSONDecodeError):
                LOGGER.warning(
                    "Discarding invalid candidate state for %s; rebuilding",
                    mint,
                )
            candidate = self.recommendations.candidates.get(key)
        if candidate is None:
            candidate = self.recommendations.add(quote, result)
        else:
            candidate = self.recommendations.update(quote)

        retained = (
            candidate is not None
            and candidate.key in self.recommendations.candidates
        )
        reason = (
            candidate.decision_reason
            if retained and candidate is not None
            else "intelligence qualified; waiting for active shortlist space"
        )
        self.store.record_auto_buy_discovery_observation(
            token_address=mint,
            symbol=symbol,
            status="TRACKING" if retained else "WATCHING",
            next_check_epoch=(
                observed + self.settings.auto_buy_watch_retry_max_seconds
            ),
            reason=reason,
            quote_available=True,
            candidate_json=(candidate.to_json() if candidate else None),
            tier=result.tier,
            intelligence_score=result.total_score,
            signal_score=(candidate.signal_score if candidate else 0),
            decision=(candidate.decision if candidate else None),
            liquidity_usd=quote.liquidity_usd,
            observed_epoch=observed,
        )
        if not watch.get("candidate_json"):
            await self._maybe_open_paper_position(launch, quote, result)

    def _auto_buy_watch_retry_delay(self, attempts: int) -> float:
        retry_bucket = min(5, max(0, attempts - 1) // 4)
        return min(
            self.settings.auto_buy_watch_retry_max_seconds,
            self.settings.auto_buy_watch_retry_base_seconds
            * (2**retry_bucket),
        )

    def _restore_auto_buy_discovery_candidates(self) -> None:
        if not (
            self.settings.auto_buy_enabled
            and self.settings.auto_buy_discovery
        ):
            return
        now = time.time()
        restored = 0
        for watch in self.store.active_auto_buy_discovery_watches(
            now_epoch=now
        ):
            payload = watch.get("candidate_json")
            if not payload:
                continue
            try:
                candidate = RecommendationCandidate.from_json(str(payload))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                self.store.mark_auto_buy_discovery_watch(
                    str(watch["token_address"]),
                    status="REVIEW",
                    reason=f"invalid persisted candidate state: {exc}",
                )
                continue
            if (
                candidate.chain != "solana"
                or candidate.mint != watch["token_address"]
                or now - candidate.updated_at
                > self.settings.recommendation_ttl_seconds
            ):
                continue
            restored += int(self.recommendations.restore(candidate))
        if restored:
            LOGGER.info(
                "Restored %d persistent auto-buy discovery candidate(s)",
                restored,
            )
