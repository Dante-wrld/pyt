"""Typing-only description of LaunchGuard's shared state.

app.py's LaunchGuard is assembled from mixins defined across the
launch_guard_* modules; each mixin calls methods on `self` that are only
implemented by *other* mixins, and reads attributes that are only set by
LaunchGuard.__init__ in app.py. mypy checks each class in isolation, so
without this it can't see that those members will exist once the mixins
are combined.

LaunchGuardState is mypy's documented pattern for this ("mixin classes
assuming a self type" - see the mypy docs section of the same name): a
Protocol listing every attribute LaunchGuard.__init__ sets and every
cross-mixin method a mixin calls, which each mixin explicitly subclasses.
It declares no behavior and is never instantiated on its own.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol

from .bitquery import BitqueryClient
from .config import Settings
from .core import PaperBroker, RiskEngine, SQLiteStore
from .execution import (
    PortfolioSignalExitPlanner,
    ProfitLadder,
    SolanaAutoBuyer,
    SolanaAutoSeller,
)
from .geckoterminal import GeckoTerminalClient
from .intelligence import CoinIntelligence
from .market import DexScreenerOracle
from .market_structure import MarketStructureScanner
from .notifications import DecisionNotifier, PortfolioNotifier, PushoverClient
from .portfolio import PortfolioAdvisor, PortfolioSignal
from .pullback_tracking import PullbackTracker
from .recommendations import RecommendationBook, RecommendationCandidate
from .strategy import AdaptiveStrategy
from .strategy_profile import StrategyProfile
from .wallet import SolanaRpc, SolanaTokenHolding


class LaunchGuardState(Protocol):
    settings: Settings
    store: SQLiteStore
    broker: PaperBroker
    risk: RiskEngine
    oracle: DexScreenerOracle
    intelligence: CoinIntelligence
    candidate_tasks: set[asyncio.Task[Any]]
    multichain_pending_count: int
    multichain_last_result: dict[str, tuple[str, int]]
    bitquery_client: BitqueryClient | None
    gecko_client: GeckoTerminalClient
    solana_momentum_last_result: dict[str, tuple[str, int]]
    recommendation_console_output: bool
    portfolio_monitor_enabled: bool
    portfolio_last_decisions: dict[str, str]
    auto_sell_dry_run_seen: set[str]
    auto_buy_dry_run_seen: set[str]
    auto_buy_lock: asyncio.Lock
    recommendations: RecommendationBook
    pullback_tracker: PullbackTracker
    notifier: DecisionNotifier | None
    portfolio_notifier: PortfolioNotifier | None
    push_client: PushoverClient | None
    strategy: AdaptiveStrategy
    structure_scanner: MarketStructureScanner
    portfolio_advisor: PortfolioAdvisor
    profit_ladder: ProfitLadder
    portfolio_signal_exit: PortfolioSignalExitPlanner
    auto_seller: SolanaAutoSeller | None
    auto_buyer: SolanaAutoBuyer | None
    strategy_profile: StrategyProfile
    entry_block_logged: dict[str, str]
    buy_signal_last_decision: dict[str, str]
    leader_holdings: dict[str, dict[str, float]]
    signal_candle_scanner: MarketStructureScanner
    signal_tag_tasks: set[asyncio.Task[None]]

    async def _maybe_auto_buy(
        self, candidate: RecommendationCandidate
    ) -> None: ...

    async def _maybe_auto_sell(
        self, signal: PortfolioSignal, balance: SolanaTokenHolding
    ) -> None: ...

    async def _monitor_auto_rebuys(
        self,
        balances_by_mint: Mapping[str, SolanaTokenHolding],
        rpc: SolanaRpc,
    ) -> None: ...
