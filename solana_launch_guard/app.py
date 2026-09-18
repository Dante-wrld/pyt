from __future__ import annotations

import argparse
import asyncio
import json
import logging
import ssl
import sys
from collections.abc import Mapping
from typing import Any

import certifi
import websockets

from .config import Settings
from .core import Launch, PaperBroker, RiskEngine, SQLiteStore
from .intelligence import CoinIntelligence
from .market import DexScreenerOracle, MarketQuote
from .recommendations import RecommendationBook, format_recommendations
from .strategy import AdaptiveStrategy
from .wallet import SolanaRpc, WalletTrade, WalletWatcher

LOGGER = logging.getLogger("solana_launch_guard")


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
        self.recommendations = RecommendationBook(
            pool_size=settings.recommendation_pool_size,
            ttl_seconds=settings.recommendation_ttl_seconds,
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
        if self.broker.has_position(mint):
            raise ValueError("an open paper position already exists for this mint")

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
        position = self.broker.open(
            launch,
            reason="FOMO_MANUAL_IMPORT",
            cost_sol=cost_usd / sol_usd,
        )
        self.strategy.register_open(position, quote, "IMPORTED")
        LOGGER.info(
            "IMPORTED FOMO POSITION %-10s mint=%s tokens=%.8g "
            "cost=$%.2f entry=$%.12g current=$%.12g",
            symbol,
            mint,
            token_amount,
            cost_usd,
            entry_price_usd,
            quote.price_sol * sol_usd,
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
                mint: str, limiter: asyncio.Semaphore = semaphore
            ) -> None:
                async with limiter:
                    quote = await self.oracle.quote(mint)
                if quote is not None:
                    self.recommendations.update(quote)

            if candidates:
                await asyncio.gather(
                    *(refresh(candidate.mint) for candidate in candidates)
                )
                ranked = self.recommendations.ranked(
                    self.settings.recommendation_limit
                )
                if ranked:
                    use_color = self.settings.color_output and sys.stderr.isatty()
                    LOGGER.info("\n%s", format_recommendations(ranked, color=use_color))

            await asyncio.sleep(self.settings.recommendation_poll_seconds)

    async def run(self, mode: str) -> None:
        tasks: list[asyncio.Task[Any]] = [
            asyncio.create_task(self.run_price_monitor())
        ]
        if mode in {"launches", "both"}:
            tasks.append(asyncio.create_task(self.run_launch_feed()))
            tasks.append(asyncio.create_task(self.run_recommendation_monitor()))

        if mode in {"copy", "both"}:
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
        choices=("launches", "copy", "both"),
        default="launches",
        help="strategy feed to run (default: launches)",
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
        "--trader-info",
        metavar="WALLET",
        help="show stored activity for a watched public wallet and exit",
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
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

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
        elif args.summary:
            print(json.dumps(store.summary(), indent=2))
        elif args.demo:
            asyncio.run(run_demo(guard))
        else:
            asyncio.run(guard.run(args.mode))
    except KeyboardInterrupt:
        LOGGER.info("Stopped by user")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        store.close()


if __name__ == "__main__":
    main()
