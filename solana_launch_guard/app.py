from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import ssl
import subprocess
import sys
import time
from collections.abc import Mapping
from typing import Any

import certifi
import websockets

from .config import Settings
from .core import Launch, PaperBroker, RiskEngine, SQLiteStore
from .intelligence import CoinIntelligence
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
    build_portfolio_snapshot,
    format_portfolio_dashboard,
    read_portfolio_snapshot,
    write_portfolio_snapshot,
)
from .recommendations import (
    RecommendationBook,
    build_snapshot,
    format_dashboard,
    format_recommendations,
    read_snapshot,
    write_snapshot,
)
from .strategy import AdaptiveStrategy
from .wallet import SolanaRpc, WalletTrade, WalletWatcher

LOGGER = logging.getLogger("solana_launch_guard")


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
        self.notifier: DecisionNotifier | None = None
        self.portfolio_notifier: PortfolioNotifier | None = None
        if settings.pushover_enabled:
            if not settings.pushover_app_token or not settings.pushover_user_key:
                raise ValueError("Pushover is enabled but credentials are missing")
            pushover_client = PushoverClient(
                app_token=settings.pushover_app_token,
                user_key=settings.pushover_user_key,
                device=settings.pushover_device,
            )
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
        for state in store.load_portfolio_states():
            self.portfolio_advisor.restore_state(
                chain=str(state["chain"]),
                token_address=str(state["token_address"]),
                peak_price=float(state["peak_price"]),
                baseline_liquidity_usd=float(
                    state["baseline_liquidity_usd"]
                ),
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
                saved = {
                    item.token_address: item
                    for item in self.store.load_owned_holdings("solana")
                }
                if wallet:
                    balances = await rpc.token_holdings(wallet)
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

                write_portfolio_snapshot(
                    self.settings.portfolio_snapshot_path,
                    build_portfolio_snapshot(
                        signals,
                        wallet=wallet,
                        poll_seconds=self.settings.portfolio_poll_seconds,
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
                    self.recommendations.update(quote)

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
            "your read-only holdings"
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
        help="import an existing Solana Fomo holding for paper management",
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
            "open a separate macOS Terminal with read-only sell guidance "
            "for current holdings"
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
    guard = LaunchGuard(settings, store)
    try:
        if args.import_fomo_mint:
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
            if guard.notifier is None:
                raise ValueError(
                    "Pushover is disabled; set PUSHOVER_ENABLED=true and "
                    "add your app token and user key"
                )
            asyncio.run(guard.notifier.send_test())
            LOGGER.info("Pushover test notification sent")
        elif args.test_high_priority_notification:
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
            asyncio.run(run_demo(guard))
        else:
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
