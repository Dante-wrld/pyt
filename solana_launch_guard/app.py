from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import shlex
import ssl
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import certifi
import websockets

from .config import Settings
from .core import Launch, PaperBroker, RiskEngine, SQLiteStore
from .execution import (
    USDC_MINT,
    BuyIntent,
    JupiterSwapClient,
    KeyringSolanaSigner,
    PortfolioSignalExitPlanner,
    ProfitLadder,
    SolanaAutoBuyer,
    SolanaAutoSeller,
    store_fomo_solana_key,
)
from .intelligence import CoinIntelligence, IntelligenceResult
from .market import DexScreenerOracle, MarketQuote
from .multichain import (
    EvmRpc,
    EvmTransfer,
    EvmWalletWatcher,
    HyperCoreFill,
    HyperCoreState,
    HyperCoreWatcher,
)
from .notifications import DecisionNotifier, PortfolioNotifier, PushoverClient
from .portfolio import (
    OwnedHolding,
    PortfolioAdvisor,
    PortfolioSignal,
    build_portfolio_snapshot,
    format_portfolio_dashboard,
    read_portfolio_snapshot,
    write_portfolio_snapshot,
)
from .recommendations import (
    RecommendationBook,
    RecommendationCandidate,
    build_snapshot,
    format_dashboard,
    format_recommendations,
    read_snapshot,
    write_snapshot,
)
from .pullback_tracking import PullbackTracker
from .strategy import AdaptiveStrategy
from .wallet import SolanaRpc, SolanaTokenHolding, WalletTrade, WalletWatcher

LOGGER = logging.getLogger("solana_launch_guard")


def auto_buy_discovery_rejection(
    candidate: RecommendationCandidate,
    settings: Settings,
    *,
    now: float | None = None,
) -> str | None:
    """Return the first reason an automatic-discovery candidate is blocked."""
    if candidate.chain != "solana":
        return "only Solana candidates can be purchased"
    if candidate.decision not in {"BUY NOW", "BUY ZONE"}:
        return "candidate does not have a final buy decision"
    if candidate.mint in settings.auto_buy_excluded_mints:
        return "mint is excluded from automatic discovery"
    if candidate.signal_score < settings.auto_buy_discovery_min_score:
        return "signal score is below the automatic-discovery minimum"
    if (
        candidate.liquidity_usd is None
        or candidate.liquidity_usd
        < settings.auto_buy_discovery_min_liquidity_usd
    ):
        return "liquidity is below the automatic-discovery minimum"
    if (
        candidate.entry_confirmation_count
        < candidate.entry_confirmation_required
    ):
        return "entry confirmation is incomplete"
    age = (time.time() if now is None else now) - candidate.updated_at
    if age > settings.auto_buy_signal_max_age_seconds:
        return "signal is stale"
    return None


def auto_rebuy_recovery_assessment(
    watch: Mapping[str, Any],
    quote: MarketQuote | None,
    settings: Settings,
    *,
    now: float | None = None,
) -> tuple[bool, str, dict[str, float]]:
    """Evaluate an opt-in post-sale recovery without predicting a rebound."""
    observed_at = time.time() if now is None else now
    age = observed_at - float(watch["sold_at_epoch"])
    if age < settings.auto_rebuy_cooldown_seconds:
        remaining = settings.auto_rebuy_cooldown_seconds - age
        return False, f"cooldown has {remaining:.0f}s remaining", {}
    if age > settings.auto_rebuy_max_watch_seconds:
        return False, "recovery watch expired", {"age_seconds": age}
    if quote is None or quote.price_usd is None or quote.price_usd <= 0:
        return False, "USD market quote unavailable", {}

    price = quote.price_usd
    exit_price = float(watch["exit_price_usd"])
    low = min(float(watch["lowest_price_usd"]), price)
    drop_pct = max(0.0, (1 - low / exit_price) * 100)
    rebound_pct = max(0.0, (price / low - 1) * 100)
    discount_pct = (1 - price / exit_price) * 100
    momentum_pct = quote.price_change_m5_pct or 0.0
    ratio = quote.buy_sell_ratio
    liquidity = quote.liquidity_usd or 0.0
    exit_liquidity = float(watch["exit_liquidity_usd"] or 0.0)
    retention_pct = (
        liquidity / exit_liquidity * 100 if exit_liquidity > 0 else 100.0
    )
    previous_price = watch.get("last_price_usd")
    rising = previous_price is not None and price > float(previous_price)
    metrics = {
        "age_seconds": age,
        "price_usd": price,
        "lowest_price_usd": low,
        "drop_pct": drop_pct,
        "rebound_pct": rebound_pct,
        "entry_discount_pct": discount_pct,
        "momentum_pct": momentum_pct,
        "buy_sell_ratio": ratio,
        "liquidity_usd": liquidity,
        "liquidity_retention_pct": retention_pct,
    }

    rejections: list[str] = []
    if drop_pct < settings.auto_rebuy_min_drop_pct:
        rejections.append(
            f"drop {drop_pct:.1f}% is below "
            f"{settings.auto_rebuy_min_drop_pct:.1f}%"
        )
    if rebound_pct < settings.auto_rebuy_min_rebound_pct:
        rejections.append(
            f"rebound {rebound_pct:.1f}% is below "
            f"{settings.auto_rebuy_min_rebound_pct:.1f}%"
        )
    if discount_pct < settings.auto_rebuy_min_entry_discount_pct:
        rejections.append(
            f"entry discount {discount_pct:.1f}% is below "
            f"{settings.auto_rebuy_min_entry_discount_pct:.1f}%"
        )
    if momentum_pct < settings.auto_rebuy_min_momentum_pct:
        rejections.append(
            f"5m momentum {momentum_pct:.1f}% is below "
            f"{settings.auto_rebuy_min_momentum_pct:.1f}%"
        )
    if ratio < settings.auto_rebuy_min_buy_sell_ratio:
        rejections.append(
            f"buyer/seller ratio {ratio:.2f}x is below "
            f"{settings.auto_rebuy_min_buy_sell_ratio:.2f}x"
        )
    if quote.buys_m5 < settings.auto_rebuy_min_buys_m5:
        rejections.append(
            f"5m buys {quote.buys_m5} are below "
            f"{settings.auto_rebuy_min_buys_m5}"
        )
    if liquidity < settings.auto_rebuy_min_liquidity_usd:
        rejections.append(
            f"liquidity ${liquidity:,.0f} is below "
            f"${settings.auto_rebuy_min_liquidity_usd:,.0f}"
        )
    if retention_pct < settings.auto_rebuy_min_liquidity_retention_pct:
        rejections.append(
            f"liquidity retention {retention_pct:.1f}% is below "
            f"{settings.auto_rebuy_min_liquidity_retention_pct:.1f}%"
        )
    if not rising:
        rejections.append("price is not rising versus the prior poll")
    if rejections:
        return False, "; ".join(rejections), metrics
    return (
        True,
        (
            f"recovery confirmed: {drop_pct:.1f}% drop, "
            f"{rebound_pct:.1f}% rebound, {momentum_pct:+.1f}% momentum"
        ),
        metrics,
    )


def _is_stock_token_symbol(
    symbol: str, stock_symbols: frozenset[str]
) -> bool:
    """Conservatively recognize direct and wrapped Robinhood stock symbols."""
    normalized = symbol.strip().casefold()
    if normalized in stock_symbols:
        return True
    return (
        len(normalized) > 2
        and normalized.startswith("w")
        and normalized.endswith("x")
        and normalized[1:-1] in stock_symbols
    )


class LaunchGuard:
    def __init__(self, settings: Settings, store: SQLiteStore) -> None:
        self.settings = settings
        self.store = store
        self.broker = PaperBroker(settings, store)
        self.risk = RiskEngine(settings)
        self.oracle = DexScreenerOracle()
        self.intelligence = CoinIntelligence(
            core_score=settings.core_intelligence_score,
            moonshot_score=settings.moonshot_intelligence_score,
            hard_min_liquidity_usd=settings.intelligence_min_liquidity_usd,
            core_min_liquidity_usd=settings.core_min_liquidity_usd,
            max_market_cap_liquidity_ratio=(
                settings.max_market_cap_liquidity_ratio
            ),
            moonshot_max_market_cap_usd=(
                settings.moonshot_max_market_cap_usd
            ),
        )
        self.candidate_tasks: set[asyncio.Task[Any]] = set()
        self.multichain_pending_count = 0
        self.multichain_last_result: dict[str, tuple[str, int]] = {}
        self.recommendation_console_output = True
        self.portfolio_monitor_enabled = False
        self.portfolio_last_decisions: dict[str, str] = {}
        self.auto_sell_dry_run_seen: set[str] = set()
        self.auto_buy_dry_run_seen: set[str] = set()
        self.auto_buy_lock = asyncio.Lock()
        self.recommendations = RecommendationBook(
            pool_size=settings.recommendation_pool_size,
            ttl_seconds=settings.recommendation_ttl_seconds,
            pullback_trigger_pct=settings.pullback_trigger_pct,
            pullback_zone_min_pct=settings.pullback_zone_min_pct,
            pullback_zone_max_pct=settings.pullback_zone_max_pct,
            pullback_started_pct=settings.pullback_started_pct,
            entry_confirmation_polls=settings.entry_confirmation_polls,
            entry_min_signal_score=settings.entry_min_signal_score,
            entry_min_liquidity_retention_pct=(
                settings.entry_min_liquidity_retention_pct
            ),
            entry_require_nonfalling_volume=(
                settings.entry_require_nonfalling_volume
            ),
            core_stop_loss_pct=settings.stop_loss_pct,
            core_take_profit_pct=settings.take_profit_pct,
            moonshot_stop_loss_pct=settings.moonshot_stop_loss_pct,
            moonshot_take_profit_pct=settings.moonshot_take_profit_pct,
            min_entry_reward_risk_ratio=(
                settings.min_entry_reward_risk_ratio
            ),
            buy_now_min_ratio=settings.buy_now_min_ratio,
            avoid_momentum_pct=settings.avoid_entry_momentum_pct,
            avoid_sell_pressure_ratio=(
                settings.avoid_entry_sell_pressure_ratio
            ),
            min_liquidity_usd=settings.intelligence_min_liquidity_usd,
        )
        self.pullback_tracker = PullbackTracker(settings.recommendation_snapshot_path)
        try:
            restored = self.pullback_tracker.restore(self.recommendations)
            if restored:
                LOGGER.info("Restored %d tracked pullback(s)", restored)
        except (OSError, ValueError) as exc:
            LOGGER.warning("Could not restore pullback tracking: %s", exc)
        self.notifier: DecisionNotifier | None = None
        self.portfolio_notifier: PortfolioNotifier | None = None
        self.push_client: PushoverClient | None = None
        if settings.pushover_enabled:
            if not settings.pushover_app_token or not settings.pushover_user_key:
                raise ValueError("Pushover is enabled but credentials are missing")
            pushover_client = PushoverClient(
                app_token=settings.pushover_app_token,
                user_key=settings.pushover_user_key,
                device=settings.pushover_device,
            )
            self.push_client = pushover_client
            self.notifier = DecisionNotifier(
                client=pushover_client,
                store=store,
                decisions=settings.pushover_alert_decisions,
                min_score=settings.pushover_min_score,
                cooldown_seconds=settings.pushover_cooldown_seconds,
                high_priority_decisions=(
                    settings.pushover_high_priority_decisions
                ),
            )
            self.portfolio_notifier = PortfolioNotifier(
                client=pushover_client,
                store=store,
                decisions=settings.pushover_portfolio_alert_decisions,
                high_priority_decisions=(
                    settings.pushover_high_priority_decisions
                ),
                cooldown_seconds=(
                    settings.pushover_portfolio_cooldown_seconds
                ),
            )
        self.strategy = AdaptiveStrategy(
            trailing_activation_pct=settings.trailing_activation_pct,
            trailing_stop_pct=settings.trailing_stop_pct,
            momentum_exit_pct=settings.momentum_exit_pct,
            sell_pressure_ratio=settings.sell_pressure_ratio,
            liquidity_drop_pct=settings.liquidity_drop_pct,
            reentry_cooldown_seconds=settings.reentry_cooldown_seconds,
            reentry_momentum_pct=settings.reentry_momentum_pct,
            reentry_buy_sell_ratio=settings.reentry_buy_sell_ratio,
            max_reentries=settings.max_reentries,
        )
        self.portfolio_advisor = PortfolioAdvisor(
            take_partial_pct=settings.take_profit_pct,
            stop_loss_pct=settings.stop_loss_pct,
            trailing_activation_pct=settings.trailing_activation_pct,
            trailing_stop_pct=settings.trailing_stop_pct,
            momentum_exit_pct=settings.momentum_exit_pct,
            sell_pressure_ratio=settings.sell_pressure_ratio,
            liquidity_drop_pct=settings.liquidity_drop_pct,
        )
        self.profit_ladder = ProfitLadder(
            principal_trigger_multiple=(
                settings.auto_sell_principal_multiple
            ),
            half_profit_trigger_multiple=(
                settings.auto_sell_half_profit_multiple
            ),
            second_stage_fraction=(
                settings.auto_sell_second_stage_fraction
            ),
        )
        self.portfolio_signal_exit = PortfolioSignalExitPlanner(
            take_partial_fraction=(
                settings.auto_sell_take_partial_fraction
            ),
            protect_profit_fraction=(
                settings.auto_sell_protect_profit_fraction
            ),
            exit_warning_fraction=(
                settings.auto_sell_exit_warning_fraction
            ),
        )
        self.auto_seller: SolanaAutoSeller | None = None
        self.auto_buyer: SolanaAutoBuyer | None = None
        if settings.auto_sell_enabled and settings.auto_sell_live:
            assert settings.solana_wallet_address is not None
            assert settings.jupiter_api_key is not None
            signer = KeyringSolanaSigner(
                expected_public_key=settings.solana_wallet_address
            )
            self.auto_seller = SolanaAutoSeller(
                client=JupiterSwapClient(api_key=settings.jupiter_api_key),
                signer=signer,
                max_price_impact_pct=(
                    settings.auto_sell_max_price_impact_pct
                ),
                max_slippage_bps=settings.auto_sell_max_slippage_bps,
                floor_percentages=settings.auto_trade_floor_percentages,
            )
        if settings.auto_buy_enabled and settings.auto_buy_live:
            assert settings.solana_wallet_address is not None
            assert settings.jupiter_api_key is not None
            buy_signer = KeyringSolanaSigner(
                expected_public_key=settings.solana_wallet_address
            )
            self.auto_buyer = SolanaAutoBuyer(
                client=JupiterSwapClient(api_key=settings.jupiter_api_key),
                signer=buy_signer,
                max_price_impact_pct=(
                    settings.auto_buy_max_price_impact_pct
                ),
                max_slippage_bps=settings.auto_buy_max_slippage_bps,
                floor_percentages=settings.auto_trade_floor_percentages,
            )
        for state in store.load_portfolio_states():
            self.portfolio_advisor.restore_state(
                chain=str(state["chain"]),
                token_address=str(state["token_address"]),
                peak_price=float(state["peak_price"]),
                baseline_liquidity_usd=float(
                    state["baseline_liquidity_usd"]
                ),
            )
        self._restore_auto_buy_discovery_candidates()

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

    def _auto_buy_watch_retry_delay(self, attempts: int) -> float:
        retry_bucket = min(5, max(0, attempts - 1) // 4)
        return min(
            self.settings.auto_buy_watch_retry_max_seconds,
            self.settings.auto_buy_watch_retry_base_seconds
            * (2**retry_bucket),
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

    async def handle_wallet_trade(self, trade: WalletTrade) -> None:
        quote = await self.oracle.quote(trade.mint)
        symbol = quote.symbol if quote else trade.mint[:6]
        price = quote.price_sol if quote else None
        inserted = self.store.save_wallet_trade(
            wallet=trade.wallet,
            signature=trade.signature,
            slot=trade.slot,
            mint=trade.mint,
            symbol=symbol,
            side=trade.side,
            token_delta=trade.token_delta,
            native_sol_delta=trade.native_sol_delta,
            observed_price_sol=price,
        )
        if not inserted:
            return

        LOGGER.info(
            "TRADER %-4s wallet=%s token=%s mint=%s amount=%.8g signature=%s",
            trade.side,
            trade.wallet,
            symbol,
            trade.mint,
            trade.token_delta,
            trade.signature,
        )

        if trade.side != "BUY":
            return

        rejection: str | None = None
        if self.broker.has_position(trade.mint):
            rejection = "position already exists"
        elif self.broker.open_count >= self.settings.max_open_positions:
            rejection = "maximum open positions reached"
        elif (
            self.broker.exposure_sol + self.settings.trade_size_sol
            > self.settings.max_total_exposure_sol + 1e-12
        ):
            rejection = "maximum total exposure would be exceeded"
        elif quote is None:
            rejection = "no SOL market quote available"
        elif (
            quote.liquidity_usd is None
            or quote.liquidity_usd < self.settings.copy_min_liquidity_usd
        ):
            rejection = (
                f"liquidity below ${self.settings.copy_min_liquidity_usd:,.0f}"
            )

        if rejection:
            LOGGER.info(
                "COPY REJECT token=%s mint=%s reason=%s",
                symbol,
                trade.mint,
                rejection,
            )
            return

        launch = Launch(
            mint=trade.mint,
            name=symbol,
            symbol=symbol,
            creator=None,
            signature=trade.signature,
            virtual_sol=None,
            virtual_tokens=None,
            market_cap_sol=None,
            creator_buy_sol=None,
            price_sol=quote.price_sol,
            received_at="",
            raw={"source": "wallet_copy", "wallet": trade.wallet},
        )
        position = self.broker.open(launch, reason=f"COPY:{trade.wallet}")
        self.strategy.register_open(position, quote, "COPY")
        LOGGER.info(
            "COPY PAPER BUY %-10s mint=%s %.6f SOL at %.12g "
            "SOL/token leader=%s",
            position.symbol,
            position.mint,
            position.cost_sol,
            position.entry_price_sol,
            trade.wallet,
        )

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

    async def run_price_monitor(self) -> None:
        LOGGER.info(
            "Adaptive price monitor active for %d restored/open positions",
            self.broker.open_count,
        )
        while True:
            open_positions = [
                position
                for position in self.broker.positions.values()
                if position.status == "OPEN"
            ]
            for position in open_positions:
                quote = await self.oracle.quote(position.mint)
                if quote is None:
                    continue

                self.strategy.ensure_open(position, quote)
                updated = self.broker.mark(position.mint, quote.price_sol)
                if updated is None:
                    continue

                pnl_pct = (
                    quote.price_sol / updated.entry_price_sol - 1
                ) * 100

                if updated.status == "OPEN":
                    decision = self.strategy.evaluate_open(updated, quote)
                    if decision.action == "SELL":
                        updated = self.broker.close(
                            updated.mint,
                            quote.price_sol,
                            decision.reason,
                        )
                        if updated is not None:
                            self.strategy.record_exit(
                                updated.mint, quote.price_sol
                            )
                else:
                    self.strategy.record_exit(updated.mint, quote.price_sol)

                if updated is None:
                    continue
                LOGGER.info(
                    "MARK %-10s mint=%s price=%.12g pnl=%+.2f%% status=%s",
                    updated.symbol,
                    updated.mint,
                    quote.price_sol,
                    pnl_pct,
                    updated.status,
                )
                if updated.status == "CLOSED":
                    LOGGER.info(
                        "ADAPTIVE PAPER SELL %-10s mint=%s reason=%s "
                        "pnl=%+.6f SOL (%+.2f%%)",
                        updated.symbol,
                        updated.mint,
                        updated.exit_reason,
                        updated.pnl_sol or 0,
                        updated.pnl_pct or 0,
                    )

            await self.evaluate_reentries()
            await asyncio.sleep(self.settings.price_poll_seconds)

    async def evaluate_reentries(self) -> None:
        closed_states = [
            state for state in self.strategy.states.values() if not state.is_open
        ]
        if not closed_states:
            return

        sol_usd = await self.oracle.sol_usd_price()
        for state in closed_states:
            quote = await self.oracle.quote(state.mint)
            if quote is None:
                continue
            decision = self.strategy.evaluate_reentry(quote)
            if decision.action != "REENTER":
                continue

            if self.broker.open_count >= self.settings.max_open_positions:
                continue
            if sol_usd is not None:
                exposure_usd = self.broker.exposure_sol * sol_usd
                reentry_usd = state.cost_sol * sol_usd
                if (
                    exposure_usd + reentry_usd
                    > self.settings.max_total_exposure_usd
                ):
                    continue

            launch = Launch(
                mint=state.mint,
                name=quote.symbol,
                symbol=quote.symbol,
                creator=None,
                signature=None,
                virtual_sol=None,
                virtual_tokens=None,
                market_cap_sol=None,
                creator_buy_sol=None,
                price_sol=quote.price_sol,
                received_at="",
                raw={"source": "adaptive_reentry"},
            )
            if state.tier == "MOONSHOT":
                take_profit = self.settings.moonshot_take_profit_pct
                stop_loss = self.settings.moonshot_stop_loss_pct
            else:
                take_profit = self.settings.take_profit_pct
                stop_loss = self.settings.stop_loss_pct

            position = self.broker.open(
                launch,
                reason=f"REENTRY:{state.tier}:{decision.reason}",
                cost_sol=state.cost_sol,
                take_profit_pct=take_profit,
                stop_loss_pct=stop_loss,
            )
            self.strategy.register_open(
                position, quote, state.tier, is_reentry=True
            )
            LOGGER.info(
                "ADAPTIVE REENTRY %-10s mint=%s cost=%.6f SOL reason=%s",
                position.symbol,
                position.mint,
                position.cost_sol,
                decision.reason,
            )

    async def import_fomo_position(
        self,
        *,
        mint: str,
        symbol: str,
        token_amount: float,
        cost_usd: float,
    ) -> None:
        if token_amount <= 0 or cost_usd <= 0:
            raise ValueError("token amount and cost USD must be positive")

        quote, sol_usd = await asyncio.gather(
            self.oracle.quote(mint),
            self.oracle.sol_usd_price(),
        )
        if quote is None:
            raise ValueError("no SOL market quote found for this mint")
        if sol_usd is None:
            raise ValueError("SOL/USD price is unavailable")

        entry_price_usd = cost_usd / token_amount
        entry_price_sol = entry_price_usd / sol_usd
        launch = Launch(
            mint=mint,
            name=symbol,
            symbol=symbol,
            creator=None,
            signature=None,
            virtual_sol=None,
            virtual_tokens=None,
            market_cap_sol=None,
            creator_buy_sol=None,
            price_sol=entry_price_sol,
            received_at="",
            raw={"source": "fomo_manual_import"},
        )
        position = self.broker.positions.get(mint)
        if position is None or position.status != "OPEN":
            position = self.broker.open(
                launch,
                reason="FOMO_MANUAL_IMPORT",
                cost_sol=cost_usd / sol_usd,
            )
            self.strategy.register_open(position, quote, "IMPORTED")
        self.store.save_owned_holding(
            OwnedHolding(
                chain="solana",
                token_address=mint,
                symbol=symbol,
                quantity=token_amount,
                entry_price=entry_price_usd,
                price_currency="USD",
                cost_amount=cost_usd,
            )
        )
        LOGGER.info(
            "SAVED READ-ONLY HOLDING %-10s mint=%s tokens=%.8g "
            "cost=$%.2f entry=$%.12g current=$%.12g",
            symbol,
            mint,
            token_amount,
            cost_usd,
            entry_price_usd,
            quote.price_sol * sol_usd,
        )

    async def run_portfolio_monitor(self) -> None:
        wallet = self.settings.solana_wallet_address
        rpc = SolanaRpc(self.settings.solana_rpc_http_url)
        LOGGER.info(
            "Read-only holdings monitor active%s (refresh %.0fs)",
            f" for {wallet}" if wallet else " for imported holdings",
            self.settings.portfolio_poll_seconds,
        )
        backoff = 1.0
        while True:
            try:
                balances_by_mint: dict[str, SolanaTokenHolding] = {}
                saved = {
                    item.token_address: item
                    for item in self.store.load_owned_holdings("solana")
                }
                if wallet:
                    balances = await rpc.token_holdings(wallet)
                    balances_by_mint = {
                        balance.mint: balance for balance in balances
                    }
                    holdings = []
                    for balance in balances:
                        basis = saved.get(balance.mint)
                        holdings.append(
                            OwnedHolding(
                                chain="solana",
                                token_address=balance.mint,
                                symbol=(
                                    basis.symbol
                                    if basis is not None
                                    else balance.mint[:8]
                                ),
                                quantity=balance.amount,
                                entry_price=(
                                    basis.entry_price if basis is not None else None
                                ),
                                price_currency=(
                                    basis.price_currency if basis is not None else None
                                ),
                                cost_amount=(
                                    basis.cost_amount if basis is not None else None
                                ),
                            )
                        )
                else:
                    holdings = list(saved.values())

                if self.settings.auto_rebuy_enabled:
                    await self._monitor_auto_rebuys(balances_by_mint, rpc)

                semaphore = asyncio.Semaphore(5)

                async def evaluate(holding: OwnedHolding):
                    async with semaphore:
                        quote = await self.oracle.quote(
                            holding.token_address, chain=holding.chain
                        )
                    return holding, quote

                results = await asyncio.gather(
                    *(evaluate(holding) for holding in holdings)
                )
                sol_usd = await self.oracle.sol_usd_price() if results else None
                signals = [
                    self.portfolio_advisor.evaluate(
                        holding, quote, sol_usd=sol_usd
                    )
                    for holding, quote in results
                ]
                for signal in signals:
                    state = self.portfolio_advisor.state_for(
                        signal.chain, signal.token_address
                    )
                    if state is not None:
                        peak_price, baseline_liquidity = state
                        self.store.save_portfolio_state(
                            chain=signal.chain,
                            token_address=signal.token_address,
                            peak_price=peak_price,
                            baseline_liquidity_usd=baseline_liquidity,
                        )
                signals = [
                    signal
                    for signal in signals
                    if signal.current_value_usd is None
                    or signal.current_value_usd
                    >= self.settings.portfolio_min_value_usd
                ]
                for signal in signals:
                    key = f"{signal.chain}:{signal.token_address.casefold()}"
                    previous = self.portfolio_last_decisions.get(key)
                    if previous != signal.decision:
                        LOGGER.info(
                            "PORTFOLIO %-14s %-10s mint=%s reason=%s",
                            signal.decision,
                            signal.symbol,
                            signal.token_address,
                            signal.reason,
                        )
                        self.portfolio_last_decisions[key] = signal.decision

                    if self.portfolio_notifier is not None:
                        try:
                            sent = await self.portfolio_notifier.maybe_send(
                                signal
                            )
                        except ConnectionError as exc:
                            LOGGER.warning(
                                "Portfolio phone alert unavailable for %s (%s)",
                                signal.symbol,
                                exc,
                            )
                        else:
                            if sent:
                                LOGGER.info(
                                    "HIGH PRIORITY PHONE ALERT %s "
                                    "decision=%s",
                                    signal.symbol,
                                    signal.decision,
                                )

                    balance = balances_by_mint.get(signal.token_address)
                    if balance is not None:
                        await self._maybe_auto_sell(signal, balance)

                write_portfolio_snapshot(
                    self.settings.portfolio_snapshot_path,
                    build_portfolio_snapshot(
                        signals,
                        wallet=wallet,
                        poll_seconds=self.settings.portfolio_poll_seconds,
                        execution_mode=(
                            "live"
                            if self.auto_seller is not None
                            else (
                                "dry-run"
                                if self.settings.auto_sell_enabled
                                else "read-only"
                            )
                        ),
                    ),
                )
                backoff = 1.0
                await asyncio.sleep(self.settings.portfolio_poll_seconds)
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError, ValueError) as exc:
                LOGGER.warning(
                    "Portfolio monitor unavailable (%s); retrying in %.0fs",
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _maybe_auto_sell(
        self, signal: PortfolioSignal, balance: SolanaTokenHolding
    ) -> None:
        if not self.settings.auto_sell_enabled:
            return
        if signal.token_address in self.settings.auto_sell_excluded_mints:
            self.store.clear_auto_sell_signal_confirmation(
                signal.token_address
            )
            return
        policy = self.store.load_auto_sell_policy(signal.token_address)
        if policy is not None and not bool(policy["armed"]):
            self.store.clear_auto_sell_signal_confirmation(
                signal.token_address
            )
            return
        holding = next(
            (
                item
                for item in self.store.load_owned_holdings("solana")
                if item.token_address == signal.token_address
            ),
            None,
        )
        intent = None
        source = "portfolio signal"
        cycle = self.store.auto_sell_cycle(signal.token_address)
        if (
            policy is not None
            and bool(policy["armed"])
            and holding is not None
            and signal.price_currency == "USD"
            and signal.current_price is not None
        ):
            intent = self.profit_ladder.plan(
                mint=signal.token_address,
                symbol=signal.symbol,
                stage=int(policy["stage"]),
                balance_raw=balance.raw_amount,
                decimals=balance.decimals,
                current_price_usd=signal.current_price,
                entry_price_usd=holding.entry_price,
                original_cost_usd=holding.cost_amount,
                cycle=cycle,
            )
            if intent is not None:
                source = "profit ladder"

        portfolio_signal_eligible = (
            intent is None
            and self.settings.auto_sell_portfolio_signals
            and signal.current_value_usd is not None
            and signal.current_value_usd
            >= self.settings.auto_sell_min_value_usd
            and signal.decision
            in {"TAKE PARTIAL", "PROTECT PROFIT", "EXIT WARNING"}
        )
        if portfolio_signal_eligible:
            confirmation = self.store.record_auto_sell_signal_confirmation(
                token_address=signal.token_address,
                decision=signal.decision,
                reason=signal.reason,
                observed_at_epoch=time.time(),
                max_gap_seconds=(
                    self.settings.auto_sell_signal_max_gap_seconds
                ),
            )
            polls = int(confirmation["consecutive_polls"])
            required = self.settings.auto_sell_signal_confirmation_polls
            if polls < required:
                LOGGER.warning(
                    "AUTO-SELL CONFIRMING %s decision=%s polls=%d/%d",
                    signal.symbol,
                    signal.decision,
                    polls,
                    required,
                )
                return
            intent = self.portfolio_signal_exit.plan(
                mint=signal.token_address,
                symbol=signal.symbol,
                decision=signal.decision,
                reason=signal.reason,
                balance_raw=balance.raw_amount,
                decimals=balance.decimals,
                cycle=cycle,
            )
        elif intent is None:
            self.store.clear_auto_sell_signal_confirmation(
                signal.token_address
            )
        if intent is None:
            return
        if self.auto_seller is None:
            if intent.event_key not in self.auto_sell_dry_run_seen:
                LOGGER.warning(
                    "AUTO-SELL READY (DRY RUN) %s source=%s reason=%s",
                    intent.symbol,
                    source,
                    intent.reason,
                )
                self.auto_sell_dry_run_seen.add(intent.event_key)
            return

        batch_key: str | None = None
        batch_full_exit = False
        if (
            source == "portfolio signal"
            and self.settings.auto_sell_adaptive_chunks
        ):
            batch_key = intent.event_key
            batch = self.store.load_or_create_auto_sell_batch(
                batch_key=batch_key,
                chain="solana",
                token_address=intent.mint,
                symbol=intent.symbol,
                stage=intent.stage,
                target_raw=intent.amount_raw,
                full_exit=intent.amount_raw >= intent.balance_raw,
            )
            if batch["status"] != "ACTIVE":
                return
            remaining_raw = int(batch["target_raw"]) - int(batch["sold_raw"])
            requested_raw = min(remaining_raw, balance.raw_amount)
            if requested_raw <= 0:
                return
            chunk_index = int(batch["next_chunk_index"])
            batch_full_exit = bool(batch["full_exit"])
            intent = replace(
                intent,
                event_key=f"{batch_key}:chunk:{chunk_index}",
                amount_raw=requested_raw,
                balance_raw=balance.raw_amount,
                reason=(
                    f"{intent.reason}; adaptive chunk {chunk_index + 1}, "
                    f"remaining target {remaining_raw} raw units"
                ),
            )

        rpc = SolanaRpc(self.settings.solana_rpc_http_url)
        try:
            if batch_key is not None:
                minimum_raw = math.ceil(
                    balance.raw_amount
                    * self.settings.auto_sell_min_chunk_fraction
                )
                simulation = await self.auto_seller.preflight_adaptive(
                    intent,
                    rpc,
                    minimum_amount_raw=minimum_raw,
                    max_attempts=(
                        self.settings.auto_sell_max_chunk_attempts
                    ),
                )
            else:
                simulation = await self.auto_seller.preflight(intent, rpc)
            prepared = simulation.prepared
        except (ConnectionError, RuntimeError, ValueError) as exc:
            LOGGER.warning(
                "AUTO-SELL NOT SUBMITTED %s source=%s (%s)",
                intent.symbol,
                source,
                exc,
            )
            return
        claimed = self.store.begin_auto_sell_execution(
            event_key=intent.event_key,
            chain="solana",
            token_address=intent.mint,
            symbol=intent.symbol,
            stage=intent.stage,
            requested_raw=prepared.input_amount_raw,
            expected_output_raw=prepared.expected_output_raw,
            balance_before_raw=intent.balance_raw,
        )
        if not claimed:
            return
        try:
            receipt = await self.auto_seller.execute(prepared)
        except (ConnectionError, RuntimeError, ValueError) as exc:
            failure_signature = getattr(exc, "signature", None)
            if batch_key is not None:
                self.store.freeze_auto_sell_chunk(
                    batch_key=batch_key,
                    event_key=intent.event_key,
                    error=str(exc),
                    signature=failure_signature,
                )
            else:
                self.store.freeze_auto_sell_execution(
                    event_key=intent.event_key,
                    error=str(exc),
                    signature=failure_signature,
                )
            LOGGER.error(
                "AUTO-SELL FROZEN FOR REVIEW %s stage=%d (%s)",
                intent.symbol,
                intent.stage,
                exc,
            )
            if self.push_client is not None:
                try:
                    await self.push_client.send(
                        title="🔴 Launch Guard: SELL NEEDS REVIEW",
                        message=(
                            f"TOKEN: {intent.symbol} • SOLANA\n"
                            f"Stage: {intent.stage + 1}\n"
                            f"No automatic retry will occur.\nReason: {exc}"
                            + (
                                f"\nSignature: {failure_signature}"
                                if failure_signature
                                else ""
                            )
                        ),
                        sound="siren",
                        priority=1,
                    )
                except ConnectionError as notification_exc:
                    LOGGER.warning(
                        "Could not send auto-sell review alert (%s)",
                        notification_exc,
                    )
            return

        batch_complete = False
        if batch_key is not None:
            batch_complete = self.store.complete_auto_sell_chunk(
                batch_key=batch_key,
                event_key=intent.event_key,
                signature=receipt.signature,
                sold_raw=receipt.input_amount_raw,
                output_usdc_raw=receipt.output_amount_raw,
            )
        else:
            self.store.complete_auto_sell_execution(
                event_key=intent.event_key,
                signature=receipt.signature,
                next_stage=intent.stage + 1,
            )
        managed_complete = (
            (source == "profit ladder" and intent.stage + 1 >= 2)
            or (
                batch_key is not None
                and batch_complete
                and batch_full_exit
            )
            or (
                batch_key is None
                and receipt.input_amount_raw >= intent.balance_raw
            )
        )
        reinvestment = self.store.record_auto_buy_sale(
            token_address=intent.mint,
            sold_raw=receipt.input_amount_raw,
            proceeds_usdc_raw=receipt.output_amount_raw,
            reinvest_pct=self.settings.auto_buy_reinvest_profit_pct,
            managed_complete=managed_complete,
        )
        if (
            self.settings.auto_rebuy_enabled
            and source == "portfolio signal"
            and intent.stage == 12
            and managed_complete
            and intent.mint not in self.settings.auto_buy_excluded_mints
        ):
            sold_raw = receipt.input_amount_raw
            proceeds_raw = receipt.output_amount_raw
            sell_signature = receipt.signature
            if batch_key is not None and batch_complete:
                completed_batch = self.store.load_auto_sell_batch(batch_key)
                if completed_batch is not None:
                    sold_raw = int(completed_batch["sold_raw"])
                    proceeds_raw = int(
                        completed_batch["proceeds_usdc_raw"]
                    )
                    sell_signature = str(
                        completed_batch["last_signature"]
                        or receipt.signature
                    )
            sold_tokens = sold_raw / (10**intent.decimals)
            if sold_tokens > 0 and proceeds_raw > 0:
                watch = self.store.start_auto_rebuy_watch(
                    token_address=intent.mint,
                    symbol=intent.symbol,
                    sell_signature=sell_signature,
                    exit_price_usd=(proceeds_raw / 1_000_000) / sold_tokens,
                    exit_liquidity_usd=signal.liquidity_usd,
                    sale_proceeds_usdc_raw=proceeds_raw,
                    sold_at_epoch=time.time(),
                    max_rebuys=self.settings.auto_rebuy_max_per_token,
                )
                if watch is not None:
                    self.store.clear_auto_sell_signal_confirmation(intent.mint)
                    LOGGER.warning(
                        "AUTO-REBUY WATCHING %s cycle=%d exit=$%.12g",
                        intent.symbol,
                        int(watch["cycle"]),
                        float(watch["exit_price_usd"]),
                    )
        LOGGER.warning(
            "AUTO-SELL CONFIRMED %s source=%s input_raw=%d "
            "adaptive_attempts=%d batch_complete=%s signature=%s",
            intent.symbol,
            source,
            receipt.input_amount_raw,
            simulation.adaptive_attempts,
            batch_complete,
            receipt.signature,
        )
        if self.push_client is not None:
            try:
                await self.push_client.send(
                    title="🟢 Launch Guard: SELL CONFIRMED",
                    message=(
                        f"TOKEN: {intent.symbol} • SOLANA\n"
                        f"Source: {source}\n"
                        f"Reason: {intent.reason}\n"
                        f"Signature: {receipt.signature}"
                    ),
                    url=f"https://solscan.io/tx/{receipt.signature}",
                    url_title="Open Solscan",
                    sound="cashregister",
                    priority=1,
                )
            except ConnectionError as notification_exc:
                LOGGER.warning(
                    "Sell confirmed, but the phone alert failed (%s)",
                    notification_exc,
                )

        if reinvestment is not None:
            LOGGER.warning(
                "AUTO-BUY PROFIT LEDGER %s realized=$%.6f reinvested=$%.6f",
                intent.symbol,
                reinvestment["profit_usdc_raw"] / 1_000_000,
                reinvestment["reinvest_credit_usdc_raw"] / 1_000_000,
            )

    async def _monitor_auto_rebuys(
        self,
        balances_by_mint: Mapping[str, SolanaTokenHolding],
        rpc: SolanaRpc,
    ) -> None:
        watches = self.store.active_auto_rebuy_watches()
        if not watches:
            return
        semaphore = asyncio.Semaphore(5)

        async def load_quote(
            watch: Mapping[str, Any],
        ) -> tuple[Mapping[str, Any], MarketQuote | None]:
            async with semaphore:
                quote = await self.oracle.quote(
                    str(watch["token_address"]), chain=str(watch["chain"])
                )
            return watch, quote

        observations = await asyncio.gather(
            *(load_quote(watch) for watch in watches)
        )
        observed_at = time.time()
        for watch, quote in observations:
            mint = str(watch["token_address"])
            symbol = str(watch["symbol"])
            if mint in self.settings.auto_buy_excluded_mints:
                self.store.expire_auto_rebuy_watch(
                    token_address=mint,
                    reason="mint is excluded from automatic buys",
                )
                continue
            wallet_balance = balances_by_mint.get(mint)
            if wallet_balance is not None and wallet_balance.raw_amount > 0:
                self.store.expire_auto_rebuy_watch(
                    token_address=mint,
                    reason=(
                        "wallet already holds this mint; cost-basis mixing "
                        "blocked"
                    ),
                )
                continue
            qualifying, reason, metrics = auto_rebuy_recovery_assessment(
                watch,
                quote,
                self.settings,
                now=observed_at,
            )
            age = observed_at - float(watch["sold_at_epoch"])
            if age > self.settings.auto_rebuy_max_watch_seconds:
                self.store.expire_auto_rebuy_watch(
                    token_address=mint,
                    reason="recovery watch expired",
                )
                continue
            if quote is None or quote.price_usd is None or quote.price_usd <= 0:
                self.store.reset_auto_rebuy_confirmation(
                    token_address=mint,
                    reason=reason,
                )
                continue
            updated = self.store.record_auto_rebuy_observation(
                token_address=mint,
                current_price_usd=quote.price_usd,
                observed_at_epoch=observed_at,
                qualifying=qualifying,
                confirmation_required=(
                    self.settings.auto_rebuy_confirmation_polls
                ),
                reason=reason,
            )
            if qualifying:
                LOGGER.warning(
                    "AUTO-REBUY CONFIRMING %s polls=%d/%d drop=%.1f%% "
                    "rebound=%.1f%%",
                    symbol,
                    int(updated["confirmation_count"]),
                    self.settings.auto_rebuy_confirmation_polls,
                    metrics["drop_pct"],
                    metrics["rebound_pct"],
                )
            if updated["status"] == "READY":
                await self._execute_auto_rebuy(updated, quote, rpc)

    async def _execute_auto_rebuy(
        self,
        watch: Mapping[str, Any],
        quote: MarketQuote,
        rpc: SolanaRpc,
    ) -> None:
        async with self.auto_buy_lock:
            await self._execute_auto_rebuy_locked(watch, quote, rpc)

    async def _execute_auto_rebuy_locked(
        self,
        watch: Mapping[str, Any],
        quote: MarketQuote,
        rpc: SolanaRpc,
    ) -> None:
        mint = str(watch["token_address"])
        symbol = str(watch["symbol"])
        seed_raw = round(
            min(
                self.settings.auto_buy_seed_size_usdc,
                self.settings.auto_rebuy_max_size_usdc,
            )
            * 1_000_000
        )
        try:
            amount_raw, funding_source = self.store.preview_auto_buy_budget(
                seed_size_usdc_raw=seed_raw,
                max_seed_buys=self.settings.auto_buy_max_seed_buys,
                max_open_positions=(
                    self.settings.auto_buy_max_open_positions
                ),
            )
        except ValueError as exc:
            LOGGER.info("AUTO-REBUY WAITING %s (%s)", symbol, exc)
            return
        event_key = f"solana:{mint}:auto-rebuy:{int(watch['cycle'])}"
        intent = BuyIntent(
            mint=mint,
            symbol=symbol,
            event_key=event_key,
            amount_usdc_raw=amount_raw,
            funding_source=funding_source,
        )
        if self.auto_buyer is None:
            if event_key not in self.auto_buy_dry_run_seen:
                LOGGER.warning(
                    "AUTO-REBUY READY (DRY RUN) %s amount=$%.2f "
                    "funding=%s reason=%s",
                    symbol,
                    amount_raw / 1_000_000,
                    funding_source,
                    watch["last_reason"],
                )
                self.auto_buy_dry_run_seen.add(event_key)
            return

        assert self.settings.solana_wallet_address is not None
        try:
            usdc = await rpc.token_balance(
                self.settings.solana_wallet_address, USDC_MINT
            )
            existing = await rpc.token_balance(
                self.settings.solana_wallet_address, mint
            )
            output_decimals = await rpc.mint_decimals(mint)
            if usdc.raw_amount < amount_raw:
                raise ValueError("wallet USDC balance is below the buy amount")
            if existing.raw_amount > 0:
                raise ValueError(
                    "wallet already holds this mint; cost-basis mixing blocked"
                )
            simulation = await self.auto_buyer.preflight(intent, rpc)
            prepared = simulation.prepared
        except (ConnectionError, RuntimeError, ValueError) as exc:
            LOGGER.warning("AUTO-REBUY NOT SUBMITTED %s (%s)", symbol, exc)
            return
        claimed = self.store.begin_auto_buy_execution(
            event_key=event_key,
            token_address=mint,
            symbol=symbol,
            funding_source=funding_source,
            input_usdc_raw=prepared.input_amount_raw,
            expected_output_raw=prepared.expected_output_raw,
        )
        if not claimed:
            return
        try:
            receipt = await self.auto_buyer.execute(prepared)
            if receipt.output_amount_raw <= 0:
                raise RuntimeError("confirmed re-buy reported no token output")
        except (ConnectionError, RuntimeError, ValueError) as exc:
            failure_signature = getattr(exc, "signature", None)
            self.store.freeze_auto_buy_execution(
                event_key=event_key,
                error=str(exc),
                signature=failure_signature,
            )
            review_reason = str(exc) + (
                f"; signature={failure_signature}"
                if failure_signature
                else ""
            )
            self.store.freeze_auto_rebuy(
                token_address=mint, error=review_reason
            )
            LOGGER.error(
                "AUTO-REBUY FROZEN FOR REVIEW %s (%s)", symbol, exc
            )
            if self.push_client is not None:
                try:
                    await self.push_client.send(
                        title="🔴 Launch Guard: RE-BUY NEEDS REVIEW",
                        message=(
                            f"TOKEN: {symbol} • SOLANA\n"
                            f"No automatic retry will occur.\nReason: {exc}"
                            + (
                                f"\nSignature: {failure_signature}"
                                if failure_signature
                                else ""
                            )
                        ),
                        sound="siren",
                        priority=1,
                    )
                except ConnectionError as notification_exc:
                    LOGGER.warning(
                        "Could not send auto-rebuy review alert (%s)",
                        notification_exc,
                    )
            return

        self.store.complete_auto_buy_execution(
            event_key=event_key,
            signature=receipt.signature,
            actual_output_raw=receipt.output_amount_raw,
            output_decimals=output_decimals,
        )
        quantity = receipt.output_amount_raw / (10**output_decimals)
        cost_usdc = receipt.input_amount_raw / 1_000_000
        self.store.save_owned_holding(
            OwnedHolding(
                chain="solana",
                token_address=mint,
                symbol=symbol,
                quantity=quantity,
                entry_price=cost_usdc / quantity,
                price_currency="USD",
                cost_amount=cost_usdc,
            )
        )
        self.store.complete_auto_rebuy(
            token_address=mint,
            buy_signature=receipt.signature,
            reset_auto_sell=True,
        )
        self.store.clear_auto_sell_signal_confirmation(mint)
        self.portfolio_advisor.restore_state(
            chain="solana",
            token_address=mint,
            peak_price=quote.price_usd or cost_usdc / quantity,
            baseline_liquidity_usd=quote.liquidity_usd or 0.0,
        )
        self.store.save_portfolio_state(
            chain="solana",
            token_address=mint,
            peak_price=quote.price_usd or cost_usdc / quantity,
            baseline_liquidity_usd=quote.liquidity_usd or 0.0,
        )
        LOGGER.warning(
            "AUTO-REBUY CONFIRMED %s amount=$%.2f cycle=%d signature=%s",
            symbol,
            cost_usdc,
            int(watch["cycle"]),
            receipt.signature,
        )
        if self.push_client is not None:
            try:
                await self.push_client.send(
                    title="🟢 Launch Guard: RE-BUY CONFIRMED",
                    message=(
                        f"TOKEN: {symbol} • SOLANA\n"
                        f"Amount: ${cost_usdc:.2f}\n"
                        f"Cycle: {int(watch['cycle'])}\n"
                        f"Signature: {receipt.signature}"
                    ),
                    url=f"https://solscan.io/tx/{receipt.signature}",
                    url_title="Open Solscan",
                    sound="cashregister",
                    priority=1,
                )
            except ConnectionError as notification_exc:
                LOGGER.warning(
                    "Re-buy confirmed, but the phone alert failed (%s)",
                    notification_exc,
                )

    async def _maybe_auto_buy(
        self, candidate: RecommendationCandidate
    ) -> None:
        async with self.auto_buy_lock:
            await self._maybe_auto_buy_locked(candidate)

    async def _maybe_auto_buy_locked(
        self, candidate: RecommendationCandidate
    ) -> None:
        if (
            not self.settings.auto_buy_enabled
            or candidate.chain != "solana"
            or candidate.decision not in {"BUY NOW", "BUY ZONE"}
            or candidate.mint in self.settings.auto_buy_excluded_mints
        ):
            return
        if self.settings.auto_buy_discovery:
            rejection = auto_buy_discovery_rejection(
                candidate, self.settings
            )
            if rejection is not None:
                return
        policy = self.store.load_auto_buy_policy(candidate.mint)
        if policy is None:
            if not self.settings.auto_buy_discovery:
                return
            try:
                self.store.arm_auto_buy(candidate.mint, candidate.symbol)
            except ValueError as exc:
                LOGGER.info(
                    "AUTO-BUY DISCOVERY SKIPPED %s (%s)",
                    candidate.symbol,
                    exc,
                )
                return
            policy = self.store.load_auto_buy_policy(candidate.mint)
            LOGGER.warning(
                "AUTO-BUY DISCOVERED %s mint=%s score=%d liquidity=$%.0f",
                candidate.symbol,
                candidate.mint,
                candidate.signal_score,
                candidate.liquidity_usd or 0,
            )
        if policy is None or not bool(policy["armed"]):
            return
        if self.settings.auto_buy_discovery:
            self.store.mark_auto_buy_discovery_watch(
                candidate.mint,
                status="QUALIFIED",
                reason=(
                    f"{candidate.decision} passed discovery gates; "
                    "awaiting budget and execution gates"
                ),
            )
        seed_raw = round(self.settings.auto_buy_seed_size_usdc * 1_000_000)
        try:
            amount_raw, funding_source = self.store.preview_auto_buy_budget(
                seed_size_usdc_raw=seed_raw,
                max_seed_buys=self.settings.auto_buy_max_seed_buys,
                max_open_positions=(
                    self.settings.auto_buy_max_open_positions
                ),
            )
        except ValueError as exc:
            LOGGER.info("AUTO-BUY WAITING %s (%s)", candidate.symbol, exc)
            return
        event_key = (
            f"solana:{candidate.mint}:auto-buy:{policy['updated_at']}"
        )
        intent = BuyIntent(
            mint=candidate.mint,
            symbol=str(policy["symbol"]),
            event_key=event_key,
            amount_usdc_raw=amount_raw,
            funding_source=funding_source,
        )
        if self.auto_buyer is None:
            if event_key not in self.auto_buy_dry_run_seen:
                LOGGER.warning(
                    "AUTO-BUY READY (DRY RUN) %s decision=%s amount=$%.2f "
                    "funding=%s",
                    intent.symbol,
                    candidate.decision,
                    amount_raw / 1_000_000,
                    funding_source,
                )
                self.auto_buy_dry_run_seen.add(event_key)
            return

        assert self.settings.solana_wallet_address is not None
        rpc = SolanaRpc(self.settings.solana_rpc_http_url)
        try:
            usdc = await rpc.token_balance(
                self.settings.solana_wallet_address, USDC_MINT
            )
            existing = await rpc.token_balance(
                self.settings.solana_wallet_address, candidate.mint
            )
            output_decimals = await rpc.mint_decimals(candidate.mint)
            if usdc.raw_amount < amount_raw:
                raise ValueError("wallet USDC balance is below the buy amount")
            if existing.raw_amount > 0:
                raise ValueError(
                    "wallet already holds this mint; cost-basis mixing blocked"
                )
            simulation = await self.auto_buyer.preflight(intent, rpc)
            prepared = simulation.prepared
        except (ConnectionError, RuntimeError, ValueError) as exc:
            LOGGER.warning("AUTO-BUY NOT SUBMITTED %s (%s)", intent.symbol, exc)
            if self.settings.auto_buy_discovery:
                self.store.mark_auto_buy_discovery_watch(
                    candidate.mint,
                    status="QUALIFIED",
                    reason=f"buy gate blocked submission: {exc}",
                )
            return
        claimed = self.store.begin_auto_buy_execution(
            event_key=event_key,
            token_address=intent.mint,
            symbol=intent.symbol,
            funding_source=funding_source,
            input_usdc_raw=prepared.input_amount_raw,
            expected_output_raw=prepared.expected_output_raw,
        )
        if not claimed:
            return
        try:
            receipt = await self.auto_buyer.execute(prepared)
            if receipt.output_amount_raw <= 0:
                raise RuntimeError("confirmed buy reported no token output")
            self.store.complete_auto_buy_execution(
                event_key=event_key,
                signature=receipt.signature,
                actual_output_raw=receipt.output_amount_raw,
                output_decimals=output_decimals,
            )
            quantity = receipt.output_amount_raw / (10**output_decimals)
            cost_usdc = receipt.input_amount_raw / 1_000_000
            self.store.save_owned_holding(
                OwnedHolding(
                    chain="solana",
                    token_address=intent.mint,
                    symbol=intent.symbol,
                    quantity=quantity,
                    entry_price=cost_usdc / quantity,
                    price_currency="USD",
                    cost_amount=cost_usdc,
                )
            )
            self.store.arm_auto_sell(intent.mint, reset_stage=True)
            self.store.clear_auto_sell_signal_confirmation(intent.mint)
        except (ConnectionError, RuntimeError, ValueError) as exc:
            self.store.freeze_auto_buy_execution(
                event_key=event_key,
                error=str(exc),
                signature=getattr(exc, "signature", None),
            )
            if self.settings.auto_buy_discovery:
                self.store.mark_auto_buy_discovery_watch(
                    candidate.mint,
                    status="REVIEW",
                    reason=f"execution requires review: {exc}",
                )
            LOGGER.error("AUTO-BUY FROZEN FOR REVIEW %s (%s)", intent.symbol, exc)
            return
        if self.settings.auto_buy_discovery:
            self.store.mark_auto_buy_discovery_watch(
                candidate.mint,
                status="BOUGHT",
                reason=f"confirmed purchase {receipt.signature}",
            )
        LOGGER.warning(
            "AUTO-BUY CONFIRMED %s amount=$%.2f signature=%s",
            intent.symbol,
            receipt.input_amount_raw / 1_000_000,
            receipt.signature,
        )

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

    async def run_multichain_feed(self, chains: tuple[str, ...]) -> None:
        LOGGER.info(
            "Multichain paper-recommendation feed active: %s "
            "(no automatic orders)",
            ", ".join(chains),
        )
        while True:
            discovery_results = await asyncio.gather(
                *(self.oracle.discover_token_profiles(chain) for chain in chains)
            )
            stock_tokens = await self.oracle.robinhood_stock_token_addresses()
            stock_symbols = await self.oracle.robinhood_stock_token_symbols()
            if (
                stock_tokens is None or stock_symbols is None
            ) and "robinhood" in chains:
                LOGGER.warning(
                    "Robinhood Stock Token registry unavailable; "
                    "skipping Robinhood candidates this pass"
                )

            work: list[tuple[str, str]] = []
            configured = self.settings.multichain_token_addresses
            for chain, discovered in zip(chains, discovery_results, strict=True):
                if chain == "robinhood" and stock_tokens is None:
                    continue
                addresses: dict[str, str] = {
                    address.casefold(): address
                    for address in configured.get(chain, ())
                }
                for address in discovered:
                    addresses.setdefault(address.casefold(), address)
                for key, address in addresses.items():
                    if chain == "robinhood" and key in (stock_tokens or ()):
                        continue
                    work.append((chain, address))

            self.multichain_pending_count = len(work)
            semaphore = asyncio.Semaphore(5)

            async def fetch(
                chain: str,
                address: str,
                limiter: asyncio.Semaphore = semaphore,
            ) -> tuple[str, str, MarketQuote | None]:
                async with limiter:
                    quote = await self.oracle.quote(address, chain=chain)
                return chain, address, quote

            try:
                quotes = await asyncio.gather(
                    *(fetch(chain, address) for chain, address in work)
                )
                for chain, address, quote in quotes:
                    if (
                        quote is not None
                        and stock_symbols is not None
                        and _is_stock_token_symbol(
                            quote.symbol, stock_symbols
                        )
                    ):
                        LOGGER.info(
                            "%s REJECT %-10s contract=%s "
                            "reason=tokenized stock symbol",
                            chain.upper(),
                            quote.symbol,
                            address,
                        )
                        continue
                    result = self.intelligence.score(quote)
                    symbol = quote.symbol if quote else address[:10]
                    result_key = f"{chain}:{address.casefold()}"
                    current_result = (result.tier, result.total_score)
                    previous_result = self.multichain_last_result.get(result_key)
                    if current_result != previous_result:
                        LOGGER.info(
                            "%s %-9s %-10s score=%d contract=%s %s",
                            chain.upper(),
                            result.tier,
                            symbol,
                            result.total_score,
                            address,
                            "; ".join(result.reasons),
                        )
                        self.multichain_last_result[result_key] = current_result

                    if quote is None or not result.accepted:
                        continue
                    self.store.save_intelligence_score(
                        mint=address,
                        symbol=symbol,
                        tier=result.tier,
                        total_score=result.total_score,
                        safety_score=result.safety_score,
                        momentum_score=result.momentum_score,
                        reasons=result.reasons,
                    )
                    self.recommendations.add(quote, result)
            finally:
                self.multichain_pending_count = 0

            await asyncio.sleep(self.settings.multichain_poll_seconds)

    async def handle_evm_transfer(self, transfer: EvmTransfer) -> None:
        quote = await self.oracle.quote(
            transfer.contract, chain=transfer.chain
        )
        price_usd = quote.price_usd if quote else None
        inserted = self.store.save_wallet_event(
            chain=transfer.chain,
            wallet=transfer.wallet,
            event_id=transfer.event_id,
            block_number=transfer.block_number,
            token_address=transfer.contract,
            symbol=transfer.symbol,
            direction=transfer.direction,
            token_amount=transfer.token_amount,
            price_usd=price_usd,
            source="EVM_TRANSFER",
        )
        if not inserted:
            return
        LOGGER.info(
            "WALLET %-4s chain=%s token=%s amount=%.8g contract=%s tx=%s",
            transfer.direction,
            transfer.chain,
            transfer.symbol,
            transfer.token_amount,
            transfer.contract,
            transfer.transaction_hash,
        )
        if quote is not None:
            result = self.intelligence.score(quote)
            if result.accepted:
                self.recommendations.add(quote, result)

    async def handle_hypercore_fill(self, fill: HyperCoreFill) -> None:
        direction = "BUY" if fill.side.upper() == "B" else "SELL"
        inserted = self.store.save_wallet_event(
            chain="hypercore",
            wallet=fill.wallet,
            event_id=fill.fill_id,
            block_number=None,
            token_address=fill.coin,
            symbol=fill.coin,
            direction=direction,
            token_amount=fill.size,
            price_usd=fill.price,
            source="HYPERCORE_FILL",
        )
        if inserted:
            LOGGER.info(
                "HYPERCORE %-4s coin=%s size=%.8g price=$%.8g fill=%s",
                direction,
                fill.coin,
                fill.size,
                fill.price,
                fill.fill_id,
            )

    async def handle_hypercore_state(self, state: HyperCoreState) -> None:
        spot = ", ".join(
            f"{coin}={amount:.8g}" for coin, amount in state.spot_balances
        ) or "none"
        perps = ", ".join(
            f"{coin}={size:+.8g}" for coin, size in state.perp_positions
        ) or "none"
        LOGGER.info("HYPERCORE HOLDINGS spot=[%s] perps=[%s]", spot, perps)

    def build_multichain_wallet_tasks(self) -> list[asyncio.Task[Any]]:
        tasks: list[asyncio.Task[Any]] = []
        wallet = self.settings.evm_wallet_address
        if wallet:
            for chain, rpc_url in self.settings.evm_rpc_urls.items():
                if not rpc_url:
                    LOGGER.warning(
                        "%s wallet monitoring inactive: RPC URL is empty", chain
                    )
                    continue
                watcher = EvmWalletWatcher(
                    chain=chain,
                    rpc=EvmRpc(rpc_url),
                    wallet=wallet,
                    callback=self.handle_evm_transfer,
                    poll_seconds=self.settings.evm_wallet_poll_seconds,
                )
                tasks.append(asyncio.create_task(watcher.run_forever()))
        else:
            LOGGER.warning(
                "No EVM_WALLET_ADDRESS configured; EVM wallet monitoring inactive"
            )

        hyperliquid = self.settings.hyperliquid_address
        if hyperliquid:
            watcher = HyperCoreWatcher(
                wallet=hyperliquid,
                fill_callback=self.handle_hypercore_fill,
                state_callback=self.handle_hypercore_state,
                poll_seconds=self.settings.evm_wallet_poll_seconds,
            )
            tasks.append(asyncio.create_task(watcher.run_forever()))
        else:
            LOGGER.warning(
                "No HYPERLIQUID_ADDRESS configured; HyperCore monitoring inactive"
            )
        return tasks

    async def run(self, mode: str) -> None:
        tasks: list[asyncio.Task[Any]] = []
        if mode != "portfolio":
            tasks.append(asyncio.create_task(self.run_price_monitor()))
        if self.portfolio_monitor_enabled:
            tasks.append(asyncio.create_task(self.run_portfolio_monitor()))
        if mode in {"launches", "both", "all"}:
            tasks.append(asyncio.create_task(self.run_launch_feed()))
            if (
                self.settings.auto_buy_enabled
                and self.settings.auto_buy_discovery
            ):
                tasks.append(
                    asyncio.create_task(
                        self.run_auto_buy_discovery_monitor()
                    )
                )

        if mode == "robinhood":
            tasks.append(
                asyncio.create_task(self.run_multichain_feed(("robinhood",)))
            )
        if mode in {"multichain", "all"}:
            tasks.append(
                asyncio.create_task(
                    self.run_multichain_feed(
                        (
                            "ethereum",
                            "base",
                            "bsc",
                            "bob",
                            "monad",
                            "robinhood",
                            "hyperevm",
                        )
                    )
                )
            )
            tasks.extend(self.build_multichain_wallet_tasks())

        if mode in {
            "launches",
            "both",
            "robinhood",
            "multichain",
            "all",
        }:
            tasks.append(asyncio.create_task(self.run_recommendation_monitor()))

        if mode in {"copy", "both", "all"}:
            if not self.settings.watched_wallets:
                if mode == "copy":
                    raise ValueError(
                        "copy mode requires WATCHED_WALLETS in .env"
                    )
                LOGGER.warning(
                    "No WATCHED_WALLETS configured; copy mode is inactive"
                )
            else:
                watcher = WalletWatcher(
                    ws_url=self.settings.solana_rpc_ws_url,
                    rpc=SolanaRpc(self.settings.solana_rpc_http_url),
                    wallets=self.settings.watched_wallets,
                    callback=self.handle_wallet_trade,
                )
                tasks.append(asyncio.create_task(watcher.run_forever()))

        await asyncio.gather(*tasks)


async def run_demo(guard: LaunchGuard) -> None:
    creation = {
        "signature": "demo-create-signature",
        "mint": "DemoMint111111111111111111111111111111111",
        "traderPublicKey": "DemoCreator11111111111111111111111111111",
        "txType": "create",
        "solAmount": 1,
        "vTokensInBondingCurve": 1_000_000_000,
        "vSolInBondingCurve": 30,
        "marketCapSol": 30,
        "name": "Demo Token",
        "symbol": "DEMO",
    }
    launch = Launch.from_payload(creation)
    quote = MarketQuote(
        mint=launch.mint,
        symbol=launch.symbol,
        price_sol=launch.price_sol or 0.00000003,
        liquidity_usd=50_000,
        market_cap_usd=100_000,
        pair_address="DemoPair",
        pair_created_at_ms=1,
        buys_m5=60,
        sells_m5=20,
        volume_m5_usd=15_000,
        price_change_m5_pct=15,
    )
    result = guard.intelligence.score(quote)
    guard.store.save_intelligence_score(
        mint=launch.mint,
        symbol=launch.symbol,
        tier=result.tier,
        total_score=result.total_score,
        safety_score=result.safety_score,
        momentum_score=result.momentum_score,
        reasons=result.reasons,
    )
    if not guard.broker.has_position(launch.mint):
        position = guard.broker.open(
            launch,
            reason=f"DEMO:{result.tier}:{result.total_score}",
        )
        guard.broker.mark(position.mint, position.entry_price_sol * 1.35)
    LOGGER.info(
        "Demo intelligence: tier=%s total=%d safety=%d momentum=%d",
        result.tier,
        result.total_score,
        result.safety_score,
        result.momentum_score,
    )
    LOGGER.info("Demo summary: %s", guard.store.summary())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor launches or wallets and simulate risk-gated trades."
    )
    parser.add_argument(
        "--mode",
        choices=(
            "launches",
            "copy",
            "both",
            "robinhood",
            "multichain",
            "all",
            "portfolio",
        ),
        default="launches",
        help=(
            "feed to run: Solana launches, wallet copy, both Solana feeds, "
            "Robinhood-only, all configured chains, every feed, or only "
            "your holdings/profit ladder"
        ),
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="run a deterministic offline paper-trade demonstration",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="print the paper-portfolio summary and exit",
    )
    parser.add_argument(
        "--test-notification",
        action="store_true",
        help="send one Pushover test notification and exit",
    )
    parser.add_argument(
        "--test-high-priority-notification",
        action="store_true",
        help="send one high-priority Pushover test and exit",
    )
    parser.add_argument(
        "--trader-info",
        metavar="WALLET",
        help="show stored activity for a watched public wallet and exit",
    )
    parser.add_argument(
        "--wallet-info",
        metavar="PUBLIC_ADDRESS",
        help="show stored multichain wallet activity and exit",
    )
    parser.add_argument(
        "--import-fomo-mint",
        metavar="MINT",
        help="import an existing Solana Fomo holding and USD cost basis",
    )
    parser.add_argument(
        "--import-symbol",
        default="IMPORTED",
        help="symbol used with --import-fomo-mint",
    )
    parser.add_argument(
        "--import-token-amount",
        type=float,
        help="token quantity currently held",
    )
    parser.add_argument(
        "--import-cost-usd",
        type=float,
        help="total USD cost basis of the currently held tokens",
    )
    parser.add_argument(
        "--recommendations-window",
        action="store_true",
        help="open a separate macOS Terminal with the live ranked watchlist",
    )
    parser.add_argument(
        "--portfolio-window",
        action="store_true",
        help=(
            "open a separate macOS Terminal with holdings guidance and "
            "profit-ladder status"
        ),
    )
    parser.add_argument(
        "--store-fomo-solana-key",
        action="store_true",
        help=(
            "securely prompt for the exported Fomo Solana key and store it "
            "in the operating-system keychain"
        ),
    )
    parser.add_argument(
        "--verify-auto-sell-signer",
        action="store_true",
        help="verify that the keychain signer matches SOLANA_WALLET_ADDRESS",
    )
    parser.add_argument(
        "--arm-auto-sell-mint",
        metavar="MINT",
        help="arm the 2x/3x automated profit ladder for one imported mint",
    )
    parser.add_argument(
        "--disarm-auto-sell-mint",
        metavar="MINT",
        help="prevent new automated sells for one mint",
    )
    parser.add_argument(
        "--allow-owned-auto-sell-mint",
        metavar="MINT",
        help="remove a per-mint block for portfolio-signal automated sells",
    )
    parser.add_argument(
        "--auto-sell-status",
        action="store_true",
        help="show armed tokens and completed profit-ladder stages",
    )
    parser.add_argument(
        "--auto-sell-review-status",
        action="store_true",
        help="show frozen or resolved-paused auto-sell review records",
    )
    parser.add_argument(
        "--reconcile-auto-sell-review",
        metavar="BATCH_KEY",
        help="compare a frozen batch with the current on-chain token balance",
    )
    parser.add_argument(
        "--resolve-auto-sell-review",
        metavar="BATCH_KEY",
        help="mark a verified no-transaction review resolved and paused",
    )
    parser.add_argument(
        "--confirm-no-transaction",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--resume-auto-sell-batch",
        metavar="BATCH_KEY",
        help="reactivate a resolved paused batch for a future monitor run",
    )
    parser.add_argument(
        "--confirm-monitor-stopped",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--preflight-auto-sell-mint",
        metavar="MINT",
        help=(
            "build, locally sign, and RPC-simulate the next ladder sale for "
            "one armed mint without broadcasting it"
        ),
    )
    parser.add_argument(
        "--preflight-owned-auto-sell-mint",
        metavar="MINT",
        help=(
            "locally sign and RPC-simulate the configured TAKE PARTIAL "
            "fraction for any owned mint without broadcasting it"
        ),
    )
    parser.add_argument(
        "--arm-auto-buy-mint",
        metavar="MINT",
        help="allow one Solana mint to receive one risk-gated automated buy",
    )
    parser.add_argument(
        "--buy-symbol",
        default="ARMED",
        help="symbol stored with --arm-auto-buy-mint",
    )
    parser.add_argument(
        "--disarm-auto-buy-mint",
        metavar="MINT",
        help="prevent a new automated buy for one mint",
    )
    parser.add_argument(
        "--auto-buy-status",
        action="store_true",
        help="show the seed counter, profit pool, allow-list, and positions",
    )
    parser.add_argument(
        "--auto-buy-watch-status",
        action="store_true",
        help="show persistent discovery watches, retry state, and outcomes",
    )
    parser.add_argument(
        "--preflight-auto-buy-mint",
        metavar="MINT",
        help=(
            "build, locally sign, and RPC-simulate an armed USDC purchase "
            "without broadcasting it"
        ),
    )
    parser.add_argument(
        "--auto-rebuy-status",
        action="store_true",
        help="show post-sale recovery watches and completed re-buys",
    )
    parser.add_argument(
        "--cancel-auto-rebuy-mint",
        metavar="MINT",
        help="cancel a watching, ready, or review-state recovery re-buy",
    )
    parser.add_argument(
        "--preflight-auto-rebuy-mint",
        metavar="MINT",
        help=(
            "build, locally sign, and RPC-simulate a watched recovery buy "
            "without broadcasting it"
        ),
    )
    parser.add_argument(
        "--recommendations-display",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--recommendations-parent-pid",
        type=int,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--portfolio-display",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--portfolio-parent-pid",
        type=int,
        help=argparse.SUPPRESS,
    )
    return parser


async def reconcile_auto_sell_review(
    settings: Settings, store: SQLiteStore, batch_key: str
) -> dict[str, Any]:
    if not settings.solana_wallet_address:
        raise ValueError(
            "auto-sell review reconciliation requires SOLANA_WALLET_ADDRESS"
        )
    review = store.load_auto_sell_review(batch_key)
    batch = review["batch"]
    execution = review["execution"]
    if batch["status"] not in {"REVIEW", "PAUSED"}:
        raise ValueError("auto-sell batch is not in review or paused")
    if execution is None:
        raise ValueError("auto-sell review has no execution record")

    rpc = SolanaRpc(settings.solana_rpc_http_url)
    balance = await rpc.token_balance(
        settings.solana_wallet_address, str(batch["token_address"])
    )
    balance_before = execution.get("balance_before_raw")
    balance_source: str | None = None
    if balance_before is not None:
        balance_before = int(balance_before)
        balance_source = "recorded_pre_execution_balance"
    elif bool(batch["full_exit"]):
        balance_before = int(batch["target_raw"]) - int(batch["sold_raw"])
        balance_source = "inferred_full_exit_remainder"

    signature = execution.get("signature")
    unchanged = (
        balance.raw_amount == balance_before
        if balance_before is not None
        else None
    )
    if signature:
        result = "SIGNATURE_REQUIRES_ON_CHAIN_REVIEW"
    elif unchanged is True:
        result = "BALANCE_UNCHANGED"
    elif unchanged is False:
        result = "BALANCE_CHANGED"
    else:
        result = "INCONCLUSIVE"
    return {
        "result": result,
        "broadcast": False,
        "batch_key": batch_key,
        "batch_status": batch["status"],
        "mint": batch["token_address"],
        "symbol": batch["symbol"],
        "execution_event_key": execution["event_key"],
        "execution_status": execution["status"],
        "execution_signature": signature,
        "execution_error": execution.get("error"),
        "requested_raw": execution["requested_raw"],
        "balance_before_raw": balance_before,
        "balance_before_source": balance_source,
        "current_balance_raw": balance.raw_amount,
        "current_balance_tokens": balance.amount,
        "balance_unchanged": unchanged,
        "eligible_to_resolve": (
            batch["status"] == "REVIEW"
            and execution["status"] == "REVIEW"
            and not signature
            and unchanged is True
        ),
    }


async def preflight_auto_sell(
    settings: Settings, store: SQLiteStore, mint: str
) -> dict[str, Any]:
    if not settings.solana_wallet_address:
        raise ValueError(
            "auto-sell preflight requires SOLANA_WALLET_ADDRESS"
        )
    if not settings.jupiter_api_key:
        raise ValueError("auto-sell preflight requires JUPITER_API_KEY")

    policy = store.load_auto_sell_policy(mint)
    if policy is None or not bool(policy["armed"]):
        raise ValueError("auto-sell preflight requires an armed token mint")
    holding = next(
        (
            item
            for item in store.load_owned_holdings("solana")
            if item.token_address == mint
        ),
        None,
    )
    if holding is None:
        raise ValueError("import the holding and USD cost basis before preflight")

    rpc = SolanaRpc(settings.solana_rpc_http_url)
    balances = await rpc.token_holdings(settings.solana_wallet_address)
    balance = next((item for item in balances if item.mint == mint), None)
    if balance is None or balance.raw_amount <= 0:
        raise ValueError("the configured wallet has no balance for this mint")

    ladder = ProfitLadder(
        principal_trigger_multiple=settings.auto_sell_principal_multiple,
        half_profit_trigger_multiple=settings.auto_sell_half_profit_multiple,
        second_stage_fraction=settings.auto_sell_second_stage_fraction,
    )
    intent = ladder.preflight_plan(
        mint=mint,
        symbol=holding.symbol,
        stage=int(policy["stage"]),
        balance_raw=balance.raw_amount,
        decimals=balance.decimals,
        entry_price_usd=holding.entry_price,
        original_cost_usd=holding.cost_amount,
    )
    signer = KeyringSolanaSigner(
        expected_public_key=settings.solana_wallet_address
    )
    seller = SolanaAutoSeller(
        client=JupiterSwapClient(api_key=settings.jupiter_api_key),
        signer=signer,
        max_price_impact_pct=settings.auto_sell_max_price_impact_pct,
        max_slippage_bps=settings.auto_sell_max_slippage_bps,
        floor_percentages=settings.auto_trade_floor_percentages,
    )
    receipt = await seller.preflight(intent, rpc)
    prepared = receipt.prepared
    return {
        "result": "PASSED",
        "broadcast": receipt.broadcast,
        "wallet": signer.public_key,
        "mint": mint,
        "symbol": holding.symbol,
        "stage": int(policy["stage"]),
        "input_amount_raw": prepared.input_amount_raw,
        "input_tokens": prepared.input_amount_raw / (10**balance.decimals),
        "expected_output_usdc": prepared.expected_output_raw / 1_000_000,
        "minimum_output_usdc": prepared.minimum_output_raw / 1_000_000,
        "price_impact_pct": prepared.price_impact_pct,
        "quoted_price_impact_pct": prepared.quoted_price_impact_pct,
        "router": prepared.router,
        "mode": prepared.mode,
        "slippage_bps": prepared.slippage_bps,
        "quoted_slippage_bps": prepared.quoted_slippage_bps,
        "reported_slippage_bps": prepared.reported_slippage_bps,
        "threshold_slippage_bps": prepared.threshold_slippage_bps,
        "fee_bps": prepared.fee_bps,
        "simulation_units_consumed": receipt.units_consumed,
        "simulation_log_count": receipt.log_count,
    }


async def preflight_owned_auto_sell(
    settings: Settings, store: SQLiteStore, mint: str
) -> dict[str, Any]:
    if not settings.solana_wallet_address:
        raise ValueError(
            "owned-token sell preflight requires SOLANA_WALLET_ADDRESS"
        )
    if not settings.jupiter_api_key:
        raise ValueError(
            "owned-token sell preflight requires JUPITER_API_KEY"
        )
    if mint in settings.auto_sell_excluded_mints:
        raise ValueError("this mint is excluded from wallet-wide auto-sell")

    rpc = SolanaRpc(settings.solana_rpc_http_url)
    balances = await rpc.token_holdings(settings.solana_wallet_address)
    balance = next((item for item in balances if item.mint == mint), None)
    if balance is None or balance.raw_amount <= 0:
        raise ValueError("the configured wallet has no balance for this mint")
    holding = next(
        (
            item
            for item in store.load_owned_holdings("solana")
            if item.token_address == mint
        ),
        None,
    )
    symbol = holding.symbol if holding is not None else mint[:8]
    planner = PortfolioSignalExitPlanner(
        take_partial_fraction=settings.auto_sell_take_partial_fraction,
        protect_profit_fraction=settings.auto_sell_protect_profit_fraction,
        exit_warning_fraction=settings.auto_sell_exit_warning_fraction,
    )
    intent = planner.plan(
        mint=mint,
        symbol=symbol,
        decision="TAKE PARTIAL",
        reason="representative wallet-wide sell preflight",
        balance_raw=balance.raw_amount,
        decimals=balance.decimals,
    )
    if intent is None:
        raise ValueError("could not build an owned-token preflight amount")
    signer = KeyringSolanaSigner(
        expected_public_key=settings.solana_wallet_address
    )
    seller = SolanaAutoSeller(
        client=JupiterSwapClient(api_key=settings.jupiter_api_key),
        signer=signer,
        max_price_impact_pct=settings.auto_sell_max_price_impact_pct,
        max_slippage_bps=settings.auto_sell_max_slippage_bps,
        floor_percentages=settings.auto_trade_floor_percentages,
    )
    if settings.auto_sell_adaptive_chunks:
        receipt = await seller.preflight_adaptive(
            intent,
            rpc,
            minimum_amount_raw=math.ceil(
                balance.raw_amount * settings.auto_sell_min_chunk_fraction
            ),
            max_attempts=settings.auto_sell_max_chunk_attempts,
        )
    else:
        receipt = await seller.preflight(intent, rpc)
    prepared = receipt.prepared
    return {
        "result": "PASSED",
        "broadcast": receipt.broadcast,
        "wallet": signer.public_key,
        "mint": mint,
        "symbol": symbol,
        "representative_rule": "TAKE PARTIAL",
        "configured_fraction": settings.auto_sell_take_partial_fraction,
        "selected_fraction": prepared.input_amount_raw / balance.raw_amount,
        "adaptive_attempts": receipt.adaptive_attempts,
        "adaptive_rejections": list(receipt.adaptive_rejections),
        "input_amount_raw": prepared.input_amount_raw,
        "input_tokens": prepared.input_amount_raw / (10**balance.decimals),
        "expected_output_usdc": prepared.expected_output_raw / 1_000_000,
        "minimum_output_usdc": prepared.minimum_output_raw / 1_000_000,
        "price_impact_pct": prepared.price_impact_pct,
        "quoted_price_impact_pct": prepared.quoted_price_impact_pct,
        "router": prepared.router,
        "mode": prepared.mode,
        "slippage_bps": prepared.slippage_bps,
        "quoted_slippage_bps": prepared.quoted_slippage_bps,
        "reported_slippage_bps": prepared.reported_slippage_bps,
        "threshold_slippage_bps": prepared.threshold_slippage_bps,
        "fee_bps": prepared.fee_bps,
        "simulation_units_consumed": receipt.units_consumed,
        "simulation_log_count": receipt.log_count,
    }


async def preflight_auto_buy(
    settings: Settings, store: SQLiteStore, mint: str
) -> dict[str, Any]:
    if not settings.solana_wallet_address:
        raise ValueError("auto-buy preflight requires SOLANA_WALLET_ADDRESS")
    if not settings.jupiter_api_key:
        raise ValueError("auto-buy preflight requires JUPITER_API_KEY")
    policy = store.load_auto_buy_policy(mint)
    if policy is None or not bool(policy["armed"]):
        raise ValueError("auto-buy preflight requires an armed token mint")

    seed_raw = round(settings.auto_buy_seed_size_usdc * 1_000_000)
    amount_raw, funding_source = store.preview_auto_buy_budget(
        seed_size_usdc_raw=seed_raw,
        max_seed_buys=settings.auto_buy_max_seed_buys,
        max_open_positions=settings.auto_buy_max_open_positions,
    )
    rpc = SolanaRpc(settings.solana_rpc_http_url)
    usdc = await rpc.token_balance(settings.solana_wallet_address, USDC_MINT)
    if usdc.raw_amount < amount_raw:
        raise ValueError("wallet USDC balance is below the preflight amount")
    existing = await rpc.token_balance(settings.solana_wallet_address, mint)
    if existing.raw_amount > 0:
        raise ValueError(
            "wallet already holds this mint; cost-basis mixing blocked"
        )
    decimals = await rpc.mint_decimals(mint)
    signer = KeyringSolanaSigner(
        expected_public_key=settings.solana_wallet_address
    )
    buyer = SolanaAutoBuyer(
        client=JupiterSwapClient(api_key=settings.jupiter_api_key),
        signer=signer,
        max_price_impact_pct=settings.auto_buy_max_price_impact_pct,
        max_slippage_bps=settings.auto_buy_max_slippage_bps,
        floor_percentages=settings.auto_trade_floor_percentages,
    )
    intent = BuyIntent(
        mint=mint,
        symbol=str(policy["symbol"]),
        event_key=f"solana:{mint}:auto-buy-preflight",
        amount_usdc_raw=amount_raw,
        funding_source=funding_source,
    )
    receipt = await buyer.preflight(intent, rpc)
    prepared = receipt.prepared
    return {
        "result": "PASSED",
        "broadcast": receipt.broadcast,
        "wallet": signer.public_key,
        "mint": mint,
        "symbol": intent.symbol,
        "funding_source": funding_source,
        "input_usdc": prepared.input_amount_raw / 1_000_000,
        "expected_output_raw": prepared.expected_output_raw,
        "expected_output_tokens": (
            prepared.expected_output_raw / (10**decimals)
        ),
        "minimum_output_raw": prepared.minimum_output_raw,
        "minimum_output_tokens": (
            prepared.minimum_output_raw / (10**decimals)
        ),
        "price_impact_pct": prepared.price_impact_pct,
        "quoted_price_impact_pct": prepared.quoted_price_impact_pct,
        "router": prepared.router,
        "mode": prepared.mode,
        "slippage_bps": prepared.slippage_bps,
        "quoted_slippage_bps": prepared.quoted_slippage_bps,
        "reported_slippage_bps": prepared.reported_slippage_bps,
        "threshold_slippage_bps": prepared.threshold_slippage_bps,
        "fee_bps": prepared.fee_bps,
        "simulation_units_consumed": receipt.units_consumed,
        "simulation_log_count": receipt.log_count,
    }


async def preflight_auto_rebuy(
    settings: Settings, store: SQLiteStore, mint: str
) -> dict[str, Any]:
    if not settings.auto_rebuy_enabled:
        raise ValueError("auto-rebuy preflight requires AUTO_REBUY_ENABLED=true")
    if not settings.solana_wallet_address:
        raise ValueError("auto-rebuy preflight requires SOLANA_WALLET_ADDRESS")
    if not settings.jupiter_api_key:
        raise ValueError("auto-rebuy preflight requires JUPITER_API_KEY")
    if mint in settings.auto_buy_excluded_mints:
        raise ValueError("this mint is excluded from automatic buys")
    watch = store.load_auto_rebuy_watch(mint)
    if watch is None or watch["status"] not in {"WATCHING", "READY"}:
        raise ValueError("auto-rebuy preflight requires an active recovery watch")

    seed_raw = round(
        min(settings.auto_buy_seed_size_usdc, settings.auto_rebuy_max_size_usdc)
        * 1_000_000
    )
    amount_raw, funding_source = store.preview_auto_buy_budget(
        seed_size_usdc_raw=seed_raw,
        max_seed_buys=settings.auto_buy_max_seed_buys,
        max_open_positions=settings.auto_buy_max_open_positions,
    )
    rpc = SolanaRpc(settings.solana_rpc_http_url)
    usdc = await rpc.token_balance(settings.solana_wallet_address, USDC_MINT)
    if usdc.raw_amount < amount_raw:
        raise ValueError("wallet USDC balance is below the preflight amount")
    existing = await rpc.token_balance(settings.solana_wallet_address, mint)
    if existing.raw_amount > 0:
        raise ValueError(
            "wallet already holds this mint; cost-basis mixing blocked"
        )
    decimals = await rpc.mint_decimals(mint)
    signer = KeyringSolanaSigner(
        expected_public_key=settings.solana_wallet_address
    )
    buyer = SolanaAutoBuyer(
        client=JupiterSwapClient(api_key=settings.jupiter_api_key),
        signer=signer,
        max_price_impact_pct=settings.auto_buy_max_price_impact_pct,
        max_slippage_bps=settings.auto_buy_max_slippage_bps,
        floor_percentages=settings.auto_trade_floor_percentages,
    )
    intent = BuyIntent(
        mint=mint,
        symbol=str(watch["symbol"]),
        event_key=f"solana:{mint}:auto-rebuy-preflight:{watch['cycle']}",
        amount_usdc_raw=amount_raw,
        funding_source=funding_source,
    )
    receipt = await buyer.preflight(intent, rpc)
    prepared = receipt.prepared
    return {
        "result": "PASSED",
        "broadcast": receipt.broadcast,
        "wallet": signer.public_key,
        "mint": mint,
        "symbol": intent.symbol,
        "watch_status": watch["status"],
        "cycle": watch["cycle"],
        "confirmation_count": watch["confirmation_count"],
        "funding_source": funding_source,
        "input_usdc": prepared.input_amount_raw / 1_000_000,
        "expected_output_raw": prepared.expected_output_raw,
        "expected_output_tokens": prepared.expected_output_raw / (10**decimals),
        "minimum_output_raw": prepared.minimum_output_raw,
        "minimum_output_tokens": prepared.minimum_output_raw / (10**decimals),
        "price_impact_pct": prepared.price_impact_pct,
        "quoted_price_impact_pct": prepared.quoted_price_impact_pct,
        "router": prepared.router,
        "mode": prepared.mode,
        "slippage_bps": prepared.slippage_bps,
        "quoted_slippage_bps": prepared.quoted_slippage_bps,
        "reported_slippage_bps": prepared.reported_slippage_bps,
        "threshold_slippage_bps": prepared.threshold_slippage_bps,
        "fee_bps": prepared.fee_bps,
        "simulation_units_consumed": receipt.units_consumed,
        "simulation_log_count": receipt.log_count,
    }


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_recommendation_display(
    snapshot_path: str, parent_pid: int | None = None
) -> None:
    try:
        while parent_pid is None or _process_exists(parent_pid):
            snapshot = read_snapshot(snapshot_path) or {
                "generated_at": 0,
                "pending_count": 0,
                "poll_seconds": 15,
                "candidates": [],
            }
            print("\033[2J\033[H", end="")
            print(format_dashboard(snapshot, color=sys.stdout.isatty()), flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def run_portfolio_display(
    snapshot_path: str, parent_pid: int | None = None
) -> None:
    try:
        while parent_pid is None or _process_exists(parent_pid):
            snapshot = read_portfolio_snapshot(snapshot_path) or {
                "generated_at": 0,
                "wallet": None,
                "poll_seconds": 15,
                "signals": [],
            }
            print("\033[2J\033[H", end="")
            print(
                format_portfolio_dashboard(
                    snapshot, color=sys.stdout.isatty()
                ),
                flush=True,
            )
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def open_recommendation_terminal(snapshot_path: str) -> None:
    if sys.platform != "darwin":
        raise ValueError(
            "--recommendations-window currently requires macOS Terminal"
        )
    command_parts = [
        sys.executable,
        "-m",
        "solana_launch_guard.app",
        "--recommendations-display",
        "--recommendations-parent-pid",
        str(os.getpid()),
    ]
    command = "cd {} && {}".format(
        shlex.quote(os.getcwd()),
        " ".join(shlex.quote(part) for part in command_parts),
    )
    script = (
        'tell application "Terminal"\n'
        "activate\n"
        f"do script {json.dumps(command)}\n"
        "end tell"
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"could not open recommendation Terminal: {exc}") from exc


def open_portfolio_terminal(snapshot_path: str) -> None:
    if sys.platform != "darwin":
        raise ValueError("--portfolio-window currently requires macOS Terminal")
    command_parts = [
        sys.executable,
        "-m",
        "solana_launch_guard.app",
        "--portfolio-display",
        "--portfolio-parent-pid",
        str(os.getpid()),
    ]
    command = "cd {} && {}".format(
        shlex.quote(os.getcwd()),
        " ".join(shlex.quote(part) for part in command_parts),
    )
    script = (
        'tell application "Terminal"\n'
        "activate\n"
        f"do script {json.dumps(command)}\n"
        "end tell"
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"could not open portfolio Terminal: {exc}") from exc


def main() -> None:
    args = build_parser().parse_args()
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.store_fomo_solana_key:
        if not settings.solana_wallet_address:
            raise SystemExit(
                "set SOLANA_WALLET_ADDRESS before storing the signer"
            )
        try:
            public_key = store_fomo_solana_key(
                expected_public_key=settings.solana_wallet_address
            )
        except (ValueError, RuntimeError) as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Stored signer for Solana wallet {public_key}")
        return
    if args.verify_auto_sell_signer:
        if not settings.solana_wallet_address:
            raise SystemExit(
                "set SOLANA_WALLET_ADDRESS before verifying the signer"
            )
        try:
            signer = KeyringSolanaSigner(
                expected_public_key=settings.solana_wallet_address
            )
        except (ValueError, RuntimeError) as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Signer verified for {signer.public_key}")
        return

    if args.recommendations_display:
        run_recommendation_display(
            settings.recommendation_snapshot_path,
            args.recommendations_parent_pid,
        )
        return
    if args.portfolio_display:
        run_portfolio_display(
            settings.portfolio_snapshot_path,
            args.portfolio_parent_pid,
        )
        return

    store = SQLiteStore(settings.database_path)
    guard: LaunchGuard | None = None
    try:
        if args.arm_auto_sell_mint:
            store.arm_auto_sell(args.arm_auto_sell_mint)
            LOGGER.info("Auto-sell armed for %s", args.arm_auto_sell_mint)
        elif args.disarm_auto_sell_mint:
            store.disarm_auto_sell(args.disarm_auto_sell_mint)
            LOGGER.info("Auto-sell disarmed for %s", args.disarm_auto_sell_mint)
        elif args.allow_owned_auto_sell_mint:
            store.allow_auto_sell_signals(args.allow_owned_auto_sell_mint)
            LOGGER.info(
                "Portfolio-signal auto-sell allowed for %s",
                args.allow_owned_auto_sell_mint,
            )
        elif args.auto_sell_status:
            print(json.dumps(store.auto_sell_status(), indent=2))
        elif args.auto_sell_review_status:
            print(json.dumps(store.auto_sell_review_status(), indent=2))
        elif args.reconcile_auto_sell_review:
            result = asyncio.run(
                reconcile_auto_sell_review(
                    settings, store, args.reconcile_auto_sell_review
                )
            )
            print("AUTO-SELL REVIEW RECONCILIATION — READ ONLY")
            print(json.dumps(result, indent=2))
        elif args.resolve_auto_sell_review:
            if settings.auto_sell_live or settings.auto_buy_live:
                raise ValueError(
                    "set AUTO_SELL_LIVE=false and AUTO_BUY_LIVE=false before "
                    "resolving a review"
                )
            if not args.confirm_no_transaction:
                raise ValueError(
                    "--confirm-no-transaction is required after checking "
                    "the wallet's on-chain history"
                )
            reconciliation = asyncio.run(
                reconcile_auto_sell_review(
                    settings, store, args.resolve_auto_sell_review
                )
            )
            if not reconciliation["eligible_to_resolve"]:
                raise ValueError(
                    "review cannot be resolved: on-chain balance is not "
                    "verified unchanged or a signature requires inspection"
                )
            batch = store.resolve_auto_sell_review(
                batch_key=args.resolve_auto_sell_review,
                confirmed_no_transaction=True,
                verified_balance_raw=int(
                    reconciliation["current_balance_raw"]
                ),
            )
            print("AUTO-SELL REVIEW RESOLVED — BATCH REMAINS PAUSED")
            print(json.dumps(batch, indent=2))
        elif args.resume_auto_sell_batch:
            if settings.auto_sell_live or settings.auto_buy_live:
                raise ValueError(
                    "set AUTO_SELL_LIVE=false and AUTO_BUY_LIVE=false before "
                    "resuming a batch"
                )
            if not args.confirm_monitor_stopped:
                raise ValueError(
                    "--confirm-monitor-stopped is required before resuming "
                    "an auto-sell batch"
                )
            batch = store.resume_auto_sell_batch(
                batch_key=args.resume_auto_sell_batch,
                confirmed_monitor_stopped=True,
            )
            print("AUTO-SELL BATCH RESUMED — NO TRANSACTION BROADCAST")
            print(json.dumps(batch, indent=2))
        elif args.preflight_auto_sell_mint:
            try:
                result = asyncio.run(
                    preflight_auto_sell(
                        settings, store, args.preflight_auto_sell_mint
                    )
                )
            except (ConnectionError, RuntimeError) as exc:
                raise ValueError(f"auto-sell preflight failed: {exc}") from exc
            print("AUTO-SELL PREFLIGHT PASSED — NO TRANSACTION BROADCAST")
            print(json.dumps(result, indent=2))
        elif args.preflight_owned_auto_sell_mint:
            try:
                result = asyncio.run(
                    preflight_owned_auto_sell(
                        settings,
                        store,
                        args.preflight_owned_auto_sell_mint,
                    )
                )
            except (ConnectionError, RuntimeError) as exc:
                raise ValueError(
                    f"owned-token auto-sell preflight failed: {exc}"
                ) from exc
            print(
                "OWNED-TOKEN AUTO-SELL PREFLIGHT PASSED — "
                "NO TRANSACTION BROADCAST"
            )
            print(json.dumps(result, indent=2))
        elif args.arm_auto_buy_mint:
            store.arm_auto_buy(args.arm_auto_buy_mint, args.buy_symbol)
            LOGGER.info(
                "Auto-buy allow-listed %s as %s",
                args.arm_auto_buy_mint,
                args.buy_symbol,
            )
        elif args.disarm_auto_buy_mint:
            store.disarm_auto_buy(args.disarm_auto_buy_mint)
            store.cancel_auto_buy_discovery_watch(args.disarm_auto_buy_mint)
            LOGGER.info("Auto-buy disarmed for %s", args.disarm_auto_buy_mint)
        elif args.auto_buy_status:
            print(json.dumps(store.auto_buy_status(), indent=2))
        elif args.auto_buy_watch_status:
            print(json.dumps(store.auto_buy_discovery_status(), indent=2))
        elif args.auto_rebuy_status:
            print(json.dumps(store.auto_rebuy_status(), indent=2))
        elif args.cancel_auto_rebuy_mint:
            cancelled = store.cancel_auto_rebuy(args.cancel_auto_rebuy_mint)
            if not cancelled:
                raise ValueError(
                    "no active or review-state auto-rebuy watch was found"
                )
            LOGGER.info(
                "Auto-rebuy cancelled for %s",
                args.cancel_auto_rebuy_mint,
            )
        elif args.preflight_auto_buy_mint:
            try:
                result = asyncio.run(
                    preflight_auto_buy(
                        settings, store, args.preflight_auto_buy_mint
                    )
                )
            except (ConnectionError, RuntimeError) as exc:
                raise ValueError(f"auto-buy preflight failed: {exc}") from exc
            print("AUTO-BUY PREFLIGHT PASSED — NO TRANSACTION BROADCAST")
            print(json.dumps(result, indent=2))
        elif args.preflight_auto_rebuy_mint:
            try:
                result = asyncio.run(
                    preflight_auto_rebuy(
                        settings, store, args.preflight_auto_rebuy_mint
                    )
                )
            except (ConnectionError, RuntimeError) as exc:
                raise ValueError(f"auto-rebuy preflight failed: {exc}") from exc
            print("AUTO-REBUY PREFLIGHT PASSED — NO TRANSACTION BROADCAST")
            print(json.dumps(result, indent=2))
        else:
            guard = LaunchGuard(settings, store)

        if args.import_fomo_mint:
            assert guard is not None
            if args.import_token_amount is None or args.import_cost_usd is None:
                raise ValueError(
                    "--import-token-amount and --import-cost-usd are required"
                )
            asyncio.run(
                guard.import_fomo_position(
                    mint=args.import_fomo_mint,
                    symbol=args.import_symbol,
                    token_amount=args.import_token_amount,
                    cost_usd=args.import_cost_usd,
                )
            )
        elif args.trader_info:
            print(json.dumps(store.trader_info(args.trader_info), indent=2))
        elif args.wallet_info:
            print(
                json.dumps(
                    store.multichain_wallet_info(args.wallet_info), indent=2
                )
            )
        elif args.test_notification:
            assert guard is not None
            if guard.notifier is None:
                raise ValueError(
                    "Pushover is disabled; set PUSHOVER_ENABLED=true and "
                    "add your app token and user key"
                )
            asyncio.run(guard.notifier.send_test())
            LOGGER.info("Pushover test notification sent")
        elif args.test_high_priority_notification:
            assert guard is not None
            if guard.notifier is None:
                raise ValueError(
                    "Pushover is disabled; set PUSHOVER_ENABLED=true and "
                    "add your app token and user key"
                )
            asyncio.run(guard.notifier.send_high_priority_test())
            LOGGER.info("High-priority Pushover test notification sent")
        elif args.summary:
            print(json.dumps(store.summary(), indent=2))
        elif args.demo:
            assert guard is not None
            asyncio.run(run_demo(guard))
        elif guard is not None:
            if args.mode == "portfolio":
                if (
                    not settings.solana_wallet_address
                    and not store.load_owned_holdings("solana")
                ):
                    raise ValueError(
                        "portfolio mode requires SOLANA_WALLET_ADDRESS "
                        "or an imported holding"
                    )
                guard.portfolio_monitor_enabled = True
            if args.recommendations_window:
                if args.mode not in {
                    "launches",
                    "both",
                    "robinhood",
                    "multichain",
                    "all",
                }:
                    raise ValueError(
                        "--recommendations-window requires a recommendation mode"
                    )
                write_snapshot(
                    settings.recommendation_snapshot_path,
                    build_snapshot(
                        [],
                        pending_count=0,
                        poll_seconds=settings.recommendation_poll_seconds,
                    ),
                )
                open_recommendation_terminal(
                    settings.recommendation_snapshot_path
                )
                guard.recommendation_console_output = False
            if args.portfolio_window:
                if (
                    not settings.solana_wallet_address
                    and not store.load_owned_holdings("solana")
                ):
                    raise ValueError(
                        "--portfolio-window requires SOLANA_WALLET_ADDRESS "
                        "or an imported holding"
                    )
                write_portfolio_snapshot(
                    settings.portfolio_snapshot_path,
                    build_portfolio_snapshot(
                        [],
                        wallet=settings.solana_wallet_address,
                        poll_seconds=settings.portfolio_poll_seconds,
                    ),
                )
                open_portfolio_terminal(settings.portfolio_snapshot_path)
                guard.portfolio_monitor_enabled = True
            asyncio.run(guard.run(args.mode))
    except KeyboardInterrupt:
        LOGGER.info("Stopped by user")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        store.close()


if __name__ == "__main__":
    main()
