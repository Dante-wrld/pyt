from __future__ import annotations

import logging
import argparse
import asyncio
import json
import math
import os
import shlex
import subprocess
import sys
import time
from dataclasses import replace
from typing import Any

from .bitquery import BitqueryClient
from .config import Settings
from .core import (
    Launch,
    PaperBroker,
    RiskEngine,
    SQLiteStore,
)
from .cost_basis import recover_usdc_basis
from .execution import (
    BuyIntent,
    JupiterSwapClient,
    KeyringSolanaSigner,
    PortfolioSignalExitPlanner,
    ProfitLadder,
    SolanaAutoBuyer,
    SolanaAutoSeller,
    USDC_MINT,
    store_fomo_solana_key,
)
from .geckoterminal import GeckoTerminalClient
from .intelligence import CoinIntelligence
from .launch_guard_auto_buy import AutoBuyMixin
from .launch_guard_auto_rebuy import AutoRebuyMixin
from .launch_guard_auto_sell import AutoSellMixin
from .launch_guard_copyfomo_monitor import CopyFomoMonitorMixin
from .launch_guard_ingestion import LaunchIngestionMixin
from .launch_guard_launchlab import LaunchLabFeedMixin
from .launch_guard_multichain import MultichainMixin
from .launch_guard_portfolio_monitor import PortfolioMonitorMixin
from .launch_guard_recommendations import RecommendationMonitorMixin
from .launch_guard_solana_momentum import SolanaMomentumFeedMixin
from .launch_guard_support import (  # noqa: F401 - re-exported for callers/tests
    _is_stock_token_symbol,
    auto_buy_discovery_rejection,
)
from .launch_guard_wallet_copy import WalletCopyMixin
from .market import (
    DexScreenerOracle,
    MarketQuote,
)
from .market_structure import MarketStructureScanner
from .notifications import (
    DecisionNotifier,
    PortfolioNotifier,
    PushoverClient,
)
from .portfolio import (
    OwnedHolding,
    PortfolioAdvisor,
    build_portfolio_snapshot,
    format_portfolio_dashboard,
    read_portfolio_snapshot,
    write_portfolio_snapshot,
)
from .pullback_tracking import PullbackTracker
from .rebuy_assessment import (  # noqa: F401 - re-exported for callers/tests
    auto_rebuy_recovery_assessment,
)
from .recommendations import (
    RecommendationBook,
    build_snapshot,
    format_dashboard,
    read_snapshot,
    write_snapshot,
)
from .strategy import AdaptiveStrategy
from .wallet import (
    SolanaRpc,
    WalletWatcher,
)

LOGGER = logging.getLogger("solana_launch_guard")


class LaunchGuard(
    LaunchIngestionMixin,
    WalletCopyMixin,
    PortfolioMonitorMixin,
    AutoSellMixin,
    AutoRebuyMixin,
    AutoBuyMixin,
    RecommendationMonitorMixin,
    MultichainMixin,
    LaunchLabFeedMixin,
    SolanaMomentumFeedMixin,
    CopyFomoMonitorMixin,
):
    """Owns shared state (settings, store, broker, risk, oracle, ...) and
    composes the trading behaviors implemented by the mixins above; see
    the ``launch_guard_*`` modules for each behavior's methods."""

    def __init__(self, settings: Settings, store: SQLiteStore) -> None:
        if os.getenv("AGENT_LIVE_CANARY_ONLY", "false").strip().lower() in {"true", "1", "yes", "on"}:
            if settings.auto_buy_live or settings.auto_rebuy_enabled:
                raise ValueError("canary-only mode requires AUTO_BUY_LIVE=false and AUTO_REBUY_ENABLED=false")
        self.settings = settings
        self.store = store
        self.broker = PaperBroker(settings, store)
        self.risk = RiskEngine(settings)
        # Bulk scanner (portfolio monitor + watchlist/discovery), not the
        # live trial's own decision path - see DexScreenerOracle's docstring
        # on max_429_retries for why this one skips the retry.
        self.oracle = DexScreenerOracle(max_429_retries=0)
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
        self.bitquery_client: BitqueryClient | None = None
        if settings.bitquery_client_id and settings.bitquery_client_secret:
            self.bitquery_client = BitqueryClient(
                client_id=settings.bitquery_client_id,
                client_secret=settings.bitquery_client_secret,
            )
        self.gecko_client = GeckoTerminalClient()
        self.solana_momentum_last_result: dict[str, tuple[str, int]] = {}
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
            pullback_reclaim_pct=settings.pullback_reclaim_pct,
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
            momentum_buy_min_ratio=settings.momentum_buy_min_ratio,
            momentum_buy_min_trades=settings.momentum_buy_min_trades,
            momentum_buy_min_liquidity_growth_pct=(
                settings.momentum_buy_min_liquidity_growth_pct
            ),
            momentum_buy_confirmation_polls=(
                settings.momentum_buy_confirmation_polls
            ),
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
                min_sell_value_usd=settings.portfolio_min_sell_value_usd,
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
        self.structure_scanner = MarketStructureScanner()
        self.portfolio_advisor = PortfolioAdvisor(
            take_partial_pct=settings.take_profit_pct,
            stop_loss_pct=settings.stop_loss_pct,
            trailing_activation_pct=settings.trailing_activation_pct,
            trailing_stop_pct=settings.trailing_stop_pct,
            small_gain_trailing_activation_pct=settings.small_gain_trailing_activation_pct,
            small_gain_trailing_stop_pct=settings.small_gain_trailing_stop_pct,
            momentum_exit_pct=settings.momentum_exit_pct,
            sell_pressure_ratio=settings.sell_pressure_ratio,
            liquidity_drop_pct=settings.liquidity_drop_pct,
            min_sell_value_usd=settings.portfolio_min_sell_value_usd,
            partial_sell_fraction=settings.auto_sell_take_partial_fraction,
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
                below_sell_minimum=bool(state["below_sell_minimum"]),
            )
        self._restore_auto_buy_discovery_candidates()

    async def _purge_restored_stock_token_candidates(self) -> None:
        """Drop pullback-restored candidates that are tokenized stocks.

        PullbackTracker.restore() runs synchronously in __init__, before
        the Robinhood stock-symbol registry can be fetched, so a candidate
        persisted to disk before that registry (or this exclusion) existed
        can still be sitting in self.recommendations.candidates here - the
        same exclusion the fresh-quote paths apply, applied once at
        startup to whatever restore() already loaded.
        """
        if not self.recommendations.candidates:
            return
        stock_symbols = await self.oracle.robinhood_stock_token_symbols()
        if stock_symbols is None:
            return
        stale = [
            key
            for key, candidate in self.recommendations.candidates.items()
            if _is_stock_token_symbol(candidate.symbol, stock_symbols)
        ]
        for key in stale:
            LOGGER.info(
                "PURGE %-10s key=%s reason=tokenized stock symbol (restored)",
                self.recommendations.candidates[key].symbol,
                key,
            )
            del self.recommendations.candidates[key]

    async def run(self, mode: str) -> None:
        await self._purge_restored_stock_token_candidates()
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
            if self.bitquery_client is not None:
                tasks.append(asyncio.create_task(self.run_launchlab_feed()))
            tasks.append(asyncio.create_task(self.run_solana_momentum_feed()))
            tasks.extend(self.build_copyfomo_wallet_tasks())

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
        "--show-all-holdings",
        action="store_true",
        help="print every holding in the latest portfolio snapshot, including small and unpriced positions",
    )
    parser.add_argument("--record-loss-sale-mint", metavar="MINT",
                        help="record a verified manual Solana sale at a net loss for advisory rebound review")
    parser.add_argument("--sale-id", help="unique transaction signature or local ID for the sale")
    parser.add_argument("--sale-symbol", help="symbol of the sold token")
    parser.add_argument("--sale-cost-usd", type=float,
                        help="allocated USD cost including buy fees")
    parser.add_argument("--sale-proceeds-usd", type=float,
                        help="net USD proceeds after sell fees")
    parser.add_argument("--sale-quantity", type=float, help="token amount sold")
    parser.add_argument("--sale-time-epoch", type=float,
                        help="actual sale timestamp in Unix seconds (default: now)")
    parser.add_argument("--loss-sales-status", action="store_true",
                        help="show recorded net-loss sales and portfolio manager rebound reviews")
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
        "--preflight-owned-auto-sell-decision",
        choices=("TAKE_PARTIAL", "SELL"),
        default="TAKE_PARTIAL",
        help="simulate either the configured partial sale or the entire owned balance",
    )
    parser.add_argument(
        "--execute-owned-sell-mint",
        metavar="MINT",
        help="one-time guarded live full exit of a named owned Solana mint",
    )
    parser.add_argument(
        "--verify-owned-sell-mint",
        metavar="MINT",
        help="read the one-time sell receipt, confirmed on-chain transaction, and current wallet balances",
    )
    parser.add_argument(
        "--recover-owned-usdc-basis-mint",
        metavar="MINT",
        help="read public wallet history and save an exact USDC-funded cost basis only when unambiguous",
    )
    parser.add_argument(
        "--confirm-owned-sell-mint",
        metavar="MINT",
        help="repeat the exact mint to authorize its one-time live sale",
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
    settings: Settings, store: SQLiteStore, mint: str, decision: str = "TAKE_PARTIAL"
) -> dict[str, Any]:
    if decision not in {"TAKE_PARTIAL", "SELL"}:
        raise ValueError("unsupported owned-token preflight decision")
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
        exit_warning_fraction=1.0,
    )
    intent = planner.plan(
        mint=mint,
        symbol=symbol,
        decision="TAKE PARTIAL" if decision == "TAKE_PARTIAL" else "EXIT WARNING",
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
    minimum_sell_value = max(
        settings.auto_sell_min_value_usd, settings.portfolio_min_sell_value_usd
    )
    minimum_sell_raw = math.ceil(minimum_sell_value * 1_000_000)
    if prepared.minimum_output_raw < minimum_sell_raw:
        raise ValueError(
            f"sell simulation passed, but minimum output "
            f"${prepared.minimum_output_raw / 1_000_000:.6f} is below "
            f"the ${minimum_sell_value:.2f} sell floor; no transaction broadcast"
        )
    return {
        "result": "PASSED",
        "broadcast": receipt.broadcast,
        "wallet": signer.public_key,
        "mint": mint,
        "symbol": symbol,
        "representative_rule": "TAKE PARTIAL" if decision == "TAKE_PARTIAL" else "SELL",
        "configured_fraction": settings.auto_sell_take_partial_fraction if decision == "TAKE_PARTIAL" else 1.0,
        "minimum_sell_value_usd": minimum_sell_value,
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


async def execute_owned_sell_once(
    settings: Settings, store: SQLiteStore, mint: str, confirmation: str | None,
    *, before_broadcast=None,
) -> dict[str, Any]:
    """Explicit single-mint exit; claim durably before any possible broadcast."""
    from .agent_live_test import CanaryJournal

    if confirmation != mint or not (32 <= len(mint) <= 44) or any(
        char not in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        for char in mint
    ):
        raise ValueError("confirm the exact Solana mint with --confirm-owned-sell-mint")
    if os.getenv("AGENT_LIVE_TEST_ENABLED", "false").lower() != "true":
        raise ValueError("AGENT_LIVE_TEST_ENABLED must be true")
    if os.getenv("AGENT_LIVE_KILL_SWITCH", "true").lower() != "false":
        raise ValueError("AGENT_LIVE_KILL_SWITCH is active")
    if not settings.solana_wallet_address or not settings.jupiter_api_key:
        raise ValueError("configured wallet and Jupiter API key are required")
    if mint in settings.auto_sell_excluded_mints:
        raise ValueError("mint is excluded from selling")
    journal = CanaryJournal(f"launch_guard_live_sell_{mint}.json")
    journal.assert_unused()
    rpc = SolanaRpc(settings.solana_rpc_http_url)
    balance = next((item for item in await rpc.token_holdings(settings.solana_wallet_address)
                    if item.mint == mint and item.raw_amount > 0), None)
    if balance is None:
        raise ValueError("configured wallet no longer holds this token")
    signer = KeyringSolanaSigner(expected_public_key=settings.solana_wallet_address)
    seller = SolanaAutoSeller(
        client=JupiterSwapClient(api_key=settings.jupiter_api_key), signer=signer,
        max_price_impact_pct=settings.auto_sell_max_price_impact_pct,
        max_slippage_bps=settings.auto_sell_max_slippage_bps,
        floor_percentages=False,
    )
    intent = PortfolioSignalExitPlanner(exit_warning_fraction=1.0).plan(
        mint=mint, symbol=mint[:8], decision="EXIT WARNING",
        reason="explicit one-time owned-token live sell", balance_raw=balance.raw_amount,
        decimals=balance.decimals,
    )
    if intent is None:
        raise ValueError("cannot construct full-balance sale")
    intent = replace(intent, event_key=f"solana:{mint}:manual-live-sell-once")
    receipt = await seller.preflight(intent, rpc)
    prepared = receipt.prepared
    floor_raw = math.ceil(max(settings.auto_sell_min_value_usd,
                              settings.portfolio_min_sell_value_usd) * 1_000_000)
    if prepared.minimum_output_raw < floor_raw:
        raise ValueError("current minimum sell output is below the sell floor")
    if prepared.expected_output_raw > 5_000_000:
        raise ValueError("single live sell test exceeds its $5 output cap")
    if (prepared.quoted_price_impact_pct is None or
        abs(prepared.quoted_price_impact_pct) > settings.auto_sell_max_price_impact_pct or
        prepared.quoted_slippage_bps is None or
        prepared.quoted_slippage_bps > settings.auto_sell_max_slippage_bps):
        raise ValueError("exact quoted impact or slippage exceeds live sell limits")
    # A second balance check catches changes during the quote and simulation.
    refreshed = next((item for item in await rpc.token_holdings(settings.solana_wallet_address)
                      if item.mint == mint), None)
    if refreshed is None or refreshed.raw_amount != balance.raw_amount:
        raise ValueError("wallet balance changed during sell simulation")
    if before_broadcast is not None:
        before_broadcast()
    attempt = {"mint": mint, "wallet": signer.public_key, "status": "PENDING",
               "at": time.time(), "input_amount_raw": prepared.input_amount_raw,
               "minimum_output_usdc": prepared.minimum_output_raw / 1_000_000}
    journal.claim(attempt)
    claimed = store.begin_auto_sell_execution(
        event_key=intent.event_key, chain="solana", token_address=mint,
        symbol=intent.symbol, stage=intent.stage,
        requested_raw=prepared.input_amount_raw,
        expected_output_raw=prepared.expected_output_raw,
        balance_before_raw=balance.raw_amount,
    )
    if not claimed:
        journal.record({"mint": mint, "status": "REVIEW_REQUIRED",
                        "reason": "database execution claim rejected"})
        raise ValueError("sale already claimed in database; inspect records before retry")
    try:
        sale = await seller.execute(prepared)
        store.complete_auto_sell_execution(
            event_key=intent.event_key, signature=sale.signature,
            next_stage=intent.stage + 1,
        )
    except (ConnectionError, RuntimeError, ValueError) as exc:
        store.freeze_auto_sell_execution(
            event_key=intent.event_key, error=str(exc),
            signature=getattr(exc, "signature", None),
        )
        journal.record({"mint": mint, "status": "REVIEW_REQUIRED",
                        "signature": getattr(exc, "signature", None), "reason": str(exc)})
        raise RuntimeError("sell outcome requires review; do not retry automatically") from exc
    result = {"mint": mint, "status": "CONFIRMED", "broadcast": True,
              "signature": sale.signature, "input_amount_raw": sale.input_amount_raw,
              "output_usdc": sale.output_amount_raw / 1_000_000}
    journal.record(result)
    return result


async def verify_owned_sell(
    settings: Settings, store: SQLiteStore, mint: str
) -> dict[str, Any]:
    """Compare the local sale receipt with confirmed chain data and wallet balances."""
    from .agent_live_test import CanaryJournal

    if not settings.solana_wallet_address:
        raise ValueError("SOLANA_WALLET_ADDRESS is required")
    journal = CanaryJournal(f"launch_guard_live_sell_{mint}.json")
    rows = journal.load()["attempts"]
    sale = next((row for row in reversed(rows)
                 if row.get("mint") == mint and row.get("status") == "CONFIRMED"
                 and row.get("signature")), None)
    if sale is None:
        raise ValueError("no confirmed local one-time sell receipt for this mint")
    rpc = SolanaRpc(settings.solana_rpc_http_url)
    transaction = await rpc.get_transaction(str(sale["signature"]))
    if not isinstance(transaction, dict) or not isinstance(transaction.get("meta"), dict):
        raise ValueError("confirmed transaction is not available from RPC yet")
    if transaction["meta"].get("err") is not None:
        raise ValueError("the on-chain transaction reports a failure")
    wallet = settings.solana_wallet_address
    if next((row.get("wallet") for row in rows if row.get("status") == "PENDING"), wallet) != wallet:
        raise ValueError("sale journal wallet does not match configured wallet")
    def raw_delta(token_mint: str) -> int:
        def amounts(key: str) -> dict[int, int]:
            return {
                int(row["accountIndex"]): int(row["uiTokenAmount"]["amount"])
                for row in transaction["meta"].get(key, [])
                if row.get("mint") == token_mint and row.get("owner") == wallet
            }
        before, after = amounts("preTokenBalances"), amounts("postTokenBalances")
        return sum(after.values()) - sum(before.values())

    sold_delta = raw_delta(mint)
    usdc_delta = raw_delta(USDC_MINT)
    if sold_delta >= 0 or -sold_delta != int(sale["input_amount_raw"]) or usdc_delta <= 0:
        raise ValueError("transaction token balance changes do not match the sell receipt")
    token = await rpc.token_balance(wallet, mint)
    usdc = await rpc.token_balance(wallet, USDC_MINT)
    result = {
        "mint": mint, "signature": sale["signature"],
        "transaction_confirmed": True,
        "wallet": wallet, "remaining_token_raw": token.raw_amount,
        "current_usdc_raw": usdc.raw_amount,
        "sold_raw_reported": sale["input_amount_raw"],
        "on_chain_token_delta_raw": sold_delta,
        "on_chain_usdc_delta_raw": usdc_delta,
        "proceeds_usdc_reported": sale["output_usdc"],
        "full_exit_currently_visible": token.raw_amount == 0,
        "note": "current USDC balance alone cannot establish proceeds if other transactions occurred",
    }
    if token.raw_amount == 0:
        holding = next((item for item in store.load_owned_holdings("solana")
                        if item.token_address == mint), None)
        pending = next((row for row in rows if row.get("status") == "PENDING"
                        and row.get("mint") == mint), None)
        decimals = await rpc.mint_decimals(mint) if holding is not None else 0
        if (holding is not None and pending is not None
            and holding.price_currency == "USD" and holding.cost_amount is not None
            and holding.quantity > 0 and
            abs(holding.quantity - int(sale["input_amount_raw"]) / 10**decimals)
                / holding.quantity < 0.01):
            if holding.cost_amount > float(sale["output_usdc"]):
                store.record_loss_sale(
                    sale_id=str(sale["signature"]), token_address=mint,
                    symbol=holding.symbol, cost_usd=holding.cost_amount,
                    proceeds_usd=float(sale["output_usdc"]),
                    quantity=holding.quantity,
                    sold_at_epoch=float(pending["at"]), source="verified_live_sell",
                )
                result["loss_sale_review_recorded"] = True
    return result


async def recover_owned_usdc_basis(
    settings: Settings, store: SQLiteStore, mint: str
) -> dict[str, Any]:
    if not settings.solana_wallet_address:
        raise ValueError("SOLANA_WALLET_ADDRESS is required")
    rpc = SolanaRpc(settings.solana_rpc_http_url)
    recovered = await recover_usdc_basis(
        rpc, wallet=settings.solana_wallet_address, mint=mint,
    )
    if recovered is None:
        raise ValueError(
            "no complete unambiguous USDC-funded basis was found in recent public "
            "wallet history; no holding data changed"
        )
    if not recovered.matches_current_holding:
        raise ValueError(
            "recovered purchases do not match the current token quantity; no "
            "holding data changed"
        )
    quote = await DexScreenerOracle().quote(mint)
    symbol = quote.symbol if quote is not None else mint[:8]
    store.save_owned_holding(OwnedHolding(
        chain="solana", token_address=mint, symbol=symbol,
        quantity=recovered.quantity, entry_price=recovered.entry_price_usd,
        price_currency="USD", cost_amount=recovered.cost_usd,
    ))
    return {
        "result": "SAVED", "source": "public_wallet_history_usdc_only",
        "mint": mint, "symbol": symbol, "quantity": recovered.quantity,
        "cost_usd": recovered.cost_usd,
        "entry_price_usd": recovered.entry_price_usd,
        "purchase_transaction_count": len(recovered.signatures),
        "signatures": list(recovered.signatures),
        "note": "SOL-funded swaps, transfers, partial sales, and incomplete history are rejected rather than estimated.",
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
    if args.show_all_holdings:
        snapshot = read_portfolio_snapshot(settings.portfolio_snapshot_path)
        if snapshot is None:
            raise SystemExit("No portfolio snapshot found; run the portfolio monitor first.")
        print(format_portfolio_dashboard(snapshot, color=sys.stdout.isatty(), show_all=True))
        return

    store = SQLiteStore(settings.database_path)
    guard: LaunchGuard | None = None
    try:
        if args.record_loss_sale_mint:
            if (not args.sale_id or not args.sale_symbol or
                    args.sale_cost_usd is None or args.sale_proceeds_usd is None or
                    args.sale_quantity is None):
                raise ValueError("recording a loss requires --sale-id, --sale-symbol, "
                                 "--sale-cost-usd, --sale-proceeds-usd, and --sale-quantity")
            print(json.dumps(store.record_loss_sale(
                sale_id=args.sale_id, token_address=args.record_loss_sale_mint,
                symbol=args.sale_symbol, cost_usd=args.sale_cost_usd,
                proceeds_usd=args.sale_proceeds_usd, quantity=args.sale_quantity,
                sold_at_epoch=args.sale_time_epoch or time.time(),
            ), indent=2))
        elif args.loss_sales_status:
            print(json.dumps(store.loss_sale_reviews(), indent=2))
        elif args.arm_auto_sell_mint:
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
                        args.preflight_owned_auto_sell_decision,
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
        elif args.execute_owned_sell_mint:
            result = asyncio.run(execute_owned_sell_once(
                settings, store, args.execute_owned_sell_mint,
                args.confirm_owned_sell_mint,
            ))
            print(json.dumps(result, indent=2))
        elif args.verify_owned_sell_mint:
            result = asyncio.run(verify_owned_sell(
                settings, store, args.verify_owned_sell_mint,
            ))
            print(json.dumps(result, indent=2))
        elif args.recover_owned_usdc_basis_mint:
            result = asyncio.run(recover_owned_usdc_basis(
                settings, store, args.recover_owned_usdc_basis_mint,
            ))
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
