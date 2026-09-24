from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _load_dotenv(path: str = ".env") -> None:
    """Load a simple .env file without overwriting real environment variables."""
    file_path = Path(path)
    if not file_path.exists():
        return
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return value


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _wallets(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    return tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))


def _addresses(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    unique: dict[str, str] = {}
    for item in raw.split(","):
        address = item.strip()
        if address:
            unique.setdefault(address.casefold(), address)
    return tuple(unique.values())


def _csv_upper(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(
        dict.fromkeys(item.strip().upper() for item in raw.split(",") if item.strip())
    )


@dataclass(frozen=True, slots=True)
class Settings:
    ws_url: str
    api_key: str | None
    trade_size_sol: float
    max_open_positions: int
    max_total_exposure_sol: float
    min_virtual_sol: float
    min_market_cap_sol: float
    max_market_cap_sol: float
    max_creator_buy_sol: float
    take_profit_pct: float
    stop_loss_pct: float
    reject_unknown_price: bool
    database_path: str
    log_level: str
    solana_rpc_http_url: str = "https://api.mainnet-beta.solana.com"
    solana_rpc_ws_url: str = "wss://api.mainnet-beta.solana.com"
    solana_wallet_address: str | None = None
    watched_wallets: tuple[str, ...] = ()
    price_poll_seconds: float = 5.0
    copy_min_liquidity_usd: float = 10_000.0
    standard_trade_size_usd: float = 10.0
    moonshot_trade_size_usd: float = 5.0
    max_total_exposure_usd: float = 30.0
    intelligence_wait_seconds: float = 30.0
    core_intelligence_score: int = 75
    moonshot_intelligence_score: int = 60
    intelligence_min_liquidity_usd: float = 5_000.0
    core_min_liquidity_usd: float = 20_000.0
    max_market_cap_liquidity_ratio: float = 30.0
    moonshot_max_market_cap_usd: float = 500_000.0
    moonshot_take_profit_pct: float = 5_000.0
    moonshot_stop_loss_pct: float = 40.0
    max_pending_candidates: int = 50
    trailing_activation_pct: float = 20.0
    trailing_stop_pct: float = 12.0
    # A gain too small to reach trailing_activation_pct above still deserves
    # protecting once it's genuinely reversing, rather than only capturing
    # profit above the bigger threshold - see PortfolioAdvisor.evaluate().
    small_gain_trailing_activation_pct: float = 8.0
    small_gain_trailing_stop_pct: float = 8.0
    momentum_exit_pct: float = -8.0
    sell_pressure_ratio: float = 1.5
    liquidity_drop_pct: float = 30.0
    reentry_cooldown_seconds: float = 120.0
    reentry_momentum_pct: float = 3.0
    reentry_buy_sell_ratio: float = 1.4
    max_reentries: int = 2
    recommendation_limit: int = 10
    recommendation_pool_size: int = 30
    recommendation_poll_seconds: float = 15.0
    recommendation_ttl_seconds: float = 1800.0
    pullback_trigger_pct: float = 8.0
    pullback_zone_min_pct: float = 4.0
    pullback_zone_max_pct: float = 6.0
    pullback_started_pct: float = 2.0
    pullback_reclaim_pct: float = 2.0
    entry_confirmation_polls: int = 2
    entry_min_signal_score: int = 65
    entry_min_liquidity_retention_pct: float = 80.0
    entry_require_nonfalling_volume: bool = True
    min_entry_reward_risk_ratio: float = 2.0
    buy_now_min_ratio: float = 1.2
    momentum_buy_min_ratio: float = 1.5
    momentum_buy_min_trades: int = 50
    momentum_buy_min_liquidity_growth_pct: float = 20.0
    avoid_entry_momentum_pct: float = -8.0
    avoid_entry_sell_pressure_ratio: float = 2.0
    pushover_enabled: bool = False
    pushover_app_token: str | None = None
    pushover_user_key: str | None = None
    pushover_device: str | None = None
    pushover_alert_decisions: tuple[str, ...] = (
        "BUY NOW",
        "BUY ZONE",
        "PULLBACK STARTED",
        "WAIT FOR PULLBACK",
        "AVOID",
    )
    pushover_min_score: int = 60
    pushover_cooldown_seconds: float = 300.0
    pushover_portfolio_alert_decisions: tuple[str, ...] = (
        "BUY MORE",
        "REBOUND WATCH",
        "STRUCTURE WATCH",
        "HOLD",
        "TAKE PARTIAL",
        "PROTECT PROFIT",
        "EXIT WARNING",
    )
    pushover_high_priority_decisions: tuple[str, ...] = (
        "BUY NOW",
        "BUY ZONE",
        "TAKE PARTIAL",
        "PROTECT PROFIT",
        "EXIT WARNING",
    )
    pushover_portfolio_cooldown_seconds: float = 300.0
    color_output: bool = True
    recommendation_snapshot_path: str = "launch_guard_recommendations.json"
    portfolio_snapshot_path: str = "launch_guard_portfolio.json"
    # Written by live_trial.py once per cycle (see _write_hunter_capacity_
    # snapshot); read by the LaunchLab/Solana-momentum feeds to throttle
    # their own metered-API polling while hunter-v1 has no room for a new
    # fresh position. Same env var name on both sides keeps them in sync
    # regardless of which one reads it via Settings vs. os.getenv directly.
    hunter_capacity_snapshot_path: str = "launch_guard_hunter_capacity.json"
    portfolio_poll_seconds: float = 15.0
    portfolio_min_value_usd: float = 0.01
    portfolio_min_sell_value_usd: float = 2.0
    auto_trade_floor_percentages: bool = False
    auto_sell_enabled: bool = False
    auto_sell_live: bool = False
    auto_sell_principal_multiple: float = 2.0
    auto_sell_half_profit_multiple: float = 3.0
    auto_sell_second_stage_fraction: float = 0.5
    auto_sell_max_price_impact_pct: float = 5.0
    auto_sell_max_slippage_bps: int = 500
    # A deliberately wider ceiling used only for a deterministic emergency
    # liquidation (hard stop-loss breach, liquidity collapse, or repeated
    # normal-ceiling exit failures on a deteriorating position) - normal
    # exits optimize execution quality, emergency exits optimize the
    # probability of getting out at all. Still bounded, not unlimited:
    # execute_live_exit's catastrophic-proceeds floor applies regardless.
    emergency_sell_max_price_impact_pct: float = 35.0
    emergency_sell_max_slippage_bps: int = 4000
    # Safety buffer (see live_trial.py's _profit_protecting_slippage_bps):
    # a profit-taking exit's dynamically-widened slippage cap stops this
    # many bps short of the position's literal breakeven point, so a fill
    # is still required to leave real profit, not just avoid an exact loss.
    profit_protecting_slippage_margin_bps: int = 200
    auto_sell_adaptive_chunks: bool = False
    auto_sell_min_chunk_fraction: float = 0.01
    auto_sell_max_chunk_attempts: int = 8
    auto_sell_portfolio_signals: bool = False
    auto_sell_take_partial_fraction: float = 0.5
    auto_sell_protect_profit_fraction: float = 1.0
    auto_sell_exit_warning_fraction: float = 1.0
    auto_sell_min_value_usd: float = 2.0
    auto_sell_signal_confirmation_polls: int = 3
    auto_sell_signal_max_gap_seconds: float = 180.0
    auto_sell_excluded_mints: tuple[str, ...] = (USDC_MINT,)
    auto_buy_enabled: bool = False
    auto_buy_live: bool = False
    auto_buy_discovery: bool = False
    auto_buy_discovery_min_score: int = 70
    auto_buy_discovery_min_liquidity_usd: float = 50_000.0
    auto_buy_discovery_min_price_vs_initial_pct: float = 20.0
    auto_buy_discovery_min_price_vs_peak_pct: float = 50.0
    auto_buy_signal_max_age_seconds: float = 30.0
    auto_buy_watch_max_seconds: float = 86_400.0
    auto_buy_watch_max_candidates: int = 250
    auto_buy_watch_batch_size: int = 10
    auto_buy_watch_retry_base_seconds: float = 15.0
    auto_buy_watch_retry_max_seconds: float = 300.0
    auto_buy_excluded_mints: tuple[str, ...] = ()
    auto_buy_seed_size_usdc: float = 5.0
    auto_buy_max_seed_buys: int = 2
    auto_buy_max_open_positions: int = 2
    auto_buy_reinvest_profit_pct: float = 50.0
    auto_buy_max_price_impact_pct: float = 5.0
    auto_buy_max_slippage_bps: int = 500
    # A wider ceiling used only for a MOMENTUM BUY-sourced entry - one that
    # already cleared the stricter momentum confirmation bar (see
    # RecommendationBook._momentum_buy_reason), unlike a calmer pullback-
    # zone entry. A token moving fast enough to earn that label can also
    # move past the normal ceiling before an approved order reaches
    # Jupiter (observed live: BETBOLT's approved buy was rejected at 2001
    # bps against a 1700 bps normal limit) - still bounded, not unlimited.
    momentum_buy_max_price_impact_pct: float = 20.0
    momentum_buy_max_slippage_bps: int = 3000
    auto_rebuy_enabled: bool = False
    auto_rebuy_cooldown_seconds: float = 600.0
    auto_rebuy_max_watch_seconds: float = 86_400.0
    auto_rebuy_min_drop_pct: float = 10.0
    auto_rebuy_min_rebound_pct: float = 5.0
    auto_rebuy_min_entry_discount_pct: float = 5.0
    auto_rebuy_min_momentum_pct: float = 3.0
    auto_rebuy_min_buy_sell_ratio: float = 1.4
    auto_rebuy_min_buys_m5: int = 8
    auto_rebuy_min_liquidity_usd: float = 20_000.0
    auto_rebuy_min_liquidity_retention_pct: float = 80.0
    auto_rebuy_confirmation_polls: int = 3
    auto_rebuy_max_per_token: int = 1
    auto_rebuy_max_size_usdc: float = 5.0
    jupiter_api_key: str | None = None
    ethereum_token_addresses: tuple[str, ...] = ()
    base_token_addresses: tuple[str, ...] = ()
    bnb_token_addresses: tuple[str, ...] = ()
    bob_token_addresses: tuple[str, ...] = ()
    monad_token_addresses: tuple[str, ...] = ()
    robinhood_token_addresses: tuple[str, ...] = ()
    hyperevm_token_addresses: tuple[str, ...] = ()
    robinhood_poll_seconds: float = 15.0
    multichain_poll_seconds: float = 15.0
    # Discovers established Solana tokens (age >= solana_momentum_min_age_days,
    # no upper bound) via GeckoTerminal's trending_pools - pump.fun/LaunchLab
    # only ever see a mint at launch, so this is the only source for a token
    # regaining momentum well after its first hours/days. 120s, not 30s: a
    # 30s cadence hit GeckoTerminal's free-tier rate limit within minutes
    # (confirmed live 2026-09-23, "HTTP 429: You've exceeded the Rate
    # Limit"), the same lesson as launchlab_poll_seconds below.
    solana_momentum_poll_seconds: float = 120.0
    solana_momentum_min_age_days: float = 3.0
    # Used instead of solana_momentum_poll_seconds while hunter-v1's own
    # fresh-position cap is full (see hunter_capacity_snapshot_path) - this
    # feed only ever produces fresh-origin candidates, so polling at normal
    # speed while nothing can act on the result just spends GeckoTerminal's
    # metered quota. Not a full stop: still slow, deliberately-infrequent
    # polling, so the pool isn't stone cold the moment a slot frees up.
    solana_momentum_throttled_poll_seconds: float = 600.0
    bitquery_client_id: str | None = None
    bitquery_client_secret: str | None = None
    # 240s, not the 15-20s the other feeds use: even after merging
    # creations/trades/pools into one combined query (5 points/poll instead
    # of 15), the Personal plan's 100k-point monthly quota was still
    # projected to run out around 2026-10-02 - over three weeks short of
    # the October 24 renewal - at the interval this started the night at
    # (60s). LaunchLab's launch volume is high enough that 240s still
    # catches meaningful activity without threatening the monthly budget.
    launchlab_poll_seconds: float = 240.0
    # Same idea and same reason as solana_momentum_throttled_poll_seconds
    # above - LaunchLab candidates are also fresh-origin only, so nothing
    # is actually missed by waiting longer here: no fresh candidate can be
    # acted on until a slot frees up regardless of how often this polls.
    # 1800s: pushed further out alongside launchlab_poll_seconds's own
    # widening, for the same monthly-budget reason - a capacity-full
    # window is the cheapest possible time to slow down further, since
    # nothing found there could be acted on immediately anyway.
    launchlab_throttled_poll_seconds: float = 1800.0
    evm_wallet_address: str | None = None
    hyperliquid_address: str | None = None
    evm_wallet_poll_seconds: float = 10.0
    # CopyFomo (a Telegram copy-trading bot) trades from its own wallet(s),
    # never Launch Guard's - read-only monitoring only (logs its trades for
    # a later weekly performance report), never scores or acts on them.
    # Which chain the EVM wallet is watched on comes from evm_rpc_urls, so
    # only that chain's own *_RPC_URL needs to be configured.
    copyfomo_solana_wallet: str | None = None
    copyfomo_evm_wallet: str | None = None
    copyfomo_evm_chain: str = "base"
    copyfomo_evm_poll_seconds: float = 15.0
    ethereum_rpc_url: str = ""
    base_rpc_url: str = ""
    bnb_rpc_url: str = ""
    bob_rpc_url: str = ""
    monad_rpc_url: str = ""
    robinhood_rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"
    hyperevm_rpc_url: str = "https://rpc.hyperliquid.xyz/evm"

    @classmethod
    def from_env(cls, dotenv_path: str = ".env") -> "Settings":
        _load_dotenv(dotenv_path)
        settings = cls(
            ws_url=os.getenv("PUMPPORTAL_WS_URL", "wss://pumpportal.fun/api/data"),
            api_key=os.getenv("PUMPPORTAL_API_KEY") or None,
            trade_size_sol=_float("PAPER_TRADE_SIZE_SOL", 0.02),
            max_open_positions=_int("MAX_OPEN_POSITIONS", 3),
            max_total_exposure_sol=_float("MAX_TOTAL_EXPOSURE_SOL", 0.06),
            min_virtual_sol=_float("MIN_VIRTUAL_SOL", 5.0),
            min_market_cap_sol=_float("MIN_MARKET_CAP_SOL", 5.0),
            max_market_cap_sol=_float("MAX_MARKET_CAP_SOL", 500.0),
            max_creator_buy_sol=_float("MAX_CREATOR_BUY_SOL", 5.0),
            take_profit_pct=_float("TAKE_PROFIT_PCT", 30.0),
            stop_loss_pct=_float("STOP_LOSS_PCT", 20.0),
            reject_unknown_price=_bool("REJECT_UNKNOWN_PRICE", True),
            database_path=os.getenv("DATABASE_PATH", "launch_guard.db"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            solana_rpc_http_url=os.getenv(
                "SOLANA_RPC_HTTP_URL", "https://api.mainnet-beta.solana.com"
            ),
            solana_rpc_ws_url=os.getenv(
                "SOLANA_RPC_WS_URL", "wss://api.mainnet-beta.solana.com"
            ),
            solana_wallet_address=(
                os.getenv("SOLANA_WALLET_ADDRESS") or None
            ),
            watched_wallets=_wallets("WATCHED_WALLETS"),
            price_poll_seconds=_float("PRICE_POLL_SECONDS", 5.0),
            copy_min_liquidity_usd=_float("COPY_MIN_LIQUIDITY_USD", 10_000.0),
            standard_trade_size_usd=_float("STANDARD_TRADE_SIZE_USD", 10.0),
            moonshot_trade_size_usd=_float("MOONSHOT_TRADE_SIZE_USD", 5.0),
            max_total_exposure_usd=_float("MAX_TOTAL_EXPOSURE_USD", 30.0),
            intelligence_wait_seconds=_float("INTELLIGENCE_WAIT_SECONDS", 30.0),
            core_intelligence_score=_int("CORE_INTELLIGENCE_SCORE", 75),
            moonshot_intelligence_score=_int("MOONSHOT_INTELLIGENCE_SCORE", 60),
            intelligence_min_liquidity_usd=_float(
                "INTELLIGENCE_MIN_LIQUIDITY_USD", 5_000.0
            ),
            core_min_liquidity_usd=_float(
                "CORE_MIN_LIQUIDITY_USD", 20_000.0
            ),
            max_market_cap_liquidity_ratio=_float(
                "MAX_MARKET_CAP_LIQUIDITY_RATIO", 30.0
            ),
            moonshot_max_market_cap_usd=_float(
                "MOONSHOT_MAX_MARKET_CAP_USD", 500_000.0
            ),
            moonshot_take_profit_pct=_float(
                "MOONSHOT_TAKE_PROFIT_PCT", 5_000.0
            ),
            moonshot_stop_loss_pct=_float("MOONSHOT_STOP_LOSS_PCT", 40.0),
            max_pending_candidates=_int("MAX_PENDING_CANDIDATES", 50),
            trailing_activation_pct=_float("TRAILING_ACTIVATION_PCT", 20.0),
            trailing_stop_pct=_float("TRAILING_STOP_PCT", 12.0),
            small_gain_trailing_activation_pct=_float(
                "SMALL_GAIN_TRAILING_ACTIVATION_PCT", 8.0
            ),
            small_gain_trailing_stop_pct=_float(
                "SMALL_GAIN_TRAILING_STOP_PCT", 8.0
            ),
            momentum_exit_pct=float(os.getenv("MOMENTUM_EXIT_PCT", "-8")),
            sell_pressure_ratio=_float("SELL_PRESSURE_RATIO", 1.5),
            liquidity_drop_pct=_float("LIQUIDITY_DROP_PCT", 30.0),
            reentry_cooldown_seconds=_float(
                "REENTRY_COOLDOWN_SECONDS", 120.0
            ),
            reentry_momentum_pct=_float("REENTRY_MOMENTUM_PCT", 3.0),
            reentry_buy_sell_ratio=_float("REENTRY_BUY_SELL_RATIO", 1.4),
            max_reentries=_int("MAX_REENTRIES", 2),
            recommendation_limit=_int("RECOMMENDATION_LIMIT", 10),
            recommendation_pool_size=_int("RECOMMENDATION_POOL_SIZE", 30),
            recommendation_poll_seconds=_float(
                "RECOMMENDATION_POLL_SECONDS", 15.0
            ),
            recommendation_ttl_seconds=_float(
                "RECOMMENDATION_TTL_SECONDS", 1800.0
            ),
            pullback_trigger_pct=_float("PULLBACK_TRIGGER_PCT", 8.0),
            pullback_zone_min_pct=_float("PULLBACK_ZONE_MIN_PCT", 4.0),
            pullback_zone_max_pct=_float("PULLBACK_ZONE_MAX_PCT", 6.0),
            pullback_started_pct=_float("PULLBACK_STARTED_PCT", 2.0),
            pullback_reclaim_pct=_float("PULLBACK_RECLAIM_PCT", 2.0),
            entry_confirmation_polls=_int("ENTRY_CONFIRMATION_POLLS", 2),
            entry_min_signal_score=_int("ENTRY_MIN_SIGNAL_SCORE", 65),
            entry_min_liquidity_retention_pct=_float(
                "ENTRY_MIN_LIQUIDITY_RETENTION_PCT", 80.0
            ),
            entry_require_nonfalling_volume=_bool(
                "ENTRY_REQUIRE_NONFALLING_VOLUME", True
            ),
            min_entry_reward_risk_ratio=_float(
                "MIN_ENTRY_REWARD_RISK_RATIO", 2.0
            ),
            buy_now_min_ratio=_float("BUY_NOW_MIN_RATIO", 1.2),
            momentum_buy_min_ratio=_float("MOMENTUM_BUY_MIN_RATIO", 1.5),
            momentum_buy_min_trades=_int("MOMENTUM_BUY_MIN_TRADES", 50),
            momentum_buy_min_liquidity_growth_pct=_float(
                "MOMENTUM_BUY_MIN_LIQUIDITY_GROWTH_PCT", 20.0
            ),
            avoid_entry_momentum_pct=float(
                os.getenv("AVOID_ENTRY_MOMENTUM_PCT", "-8")
            ),
            avoid_entry_sell_pressure_ratio=_float(
                "AVOID_ENTRY_SELL_PRESSURE_RATIO", 2.0
            ),
            pushover_enabled=_bool("PUSHOVER_ENABLED", False),
            pushover_app_token=os.getenv("PUSHOVER_APP_TOKEN") or None,
            pushover_user_key=os.getenv("PUSHOVER_USER_KEY") or None,
            pushover_device=os.getenv("PUSHOVER_DEVICE") or None,
            pushover_alert_decisions=_csv_upper(
                "PUSHOVER_ALERT_DECISIONS",
                "BUY NOW,BUY ZONE,PULLBACK STARTED,WAIT FOR PULLBACK,AVOID",
            ),
            pushover_min_score=_int("PUSHOVER_MIN_SCORE", 60),
            pushover_cooldown_seconds=_float(
                "PUSHOVER_COOLDOWN_SECONDS", 300.0
            ),
            pushover_portfolio_alert_decisions=_csv_upper(
                "PUSHOVER_PORTFOLIO_ALERT_DECISIONS",
                "BUY MORE,REBOUND WATCH,STRUCTURE WATCH,HOLD,TAKE PARTIAL,PROTECT PROFIT,EXIT WARNING",
            ),
            pushover_high_priority_decisions=_csv_upper(
                "PUSHOVER_HIGH_PRIORITY_DECISIONS",
                (
                    "BUY NOW,BUY ZONE,TAKE PARTIAL,PROTECT PROFIT,"
                    "EXIT WARNING"
                ),
            ),
            pushover_portfolio_cooldown_seconds=_float(
                "PUSHOVER_PORTFOLIO_COOLDOWN_SECONDS", 300.0
            ),
            color_output=_bool("COLOR_OUTPUT", True),
            recommendation_snapshot_path=os.getenv(
                "RECOMMENDATION_SNAPSHOT_PATH",
                "launch_guard_recommendations.json",
            ),
            portfolio_snapshot_path=os.getenv(
                "PORTFOLIO_SNAPSHOT_PATH", "launch_guard_portfolio.json"
            ),
            hunter_capacity_snapshot_path=os.getenv(
                "HUNTER_CAPACITY_SNAPSHOT_PATH", "launch_guard_hunter_capacity.json"
            ),
            portfolio_poll_seconds=_float("PORTFOLIO_POLL_SECONDS", 15.0),
            portfolio_min_value_usd=_float(
                "PORTFOLIO_MIN_VALUE_USD", 0.01
            ),
            portfolio_min_sell_value_usd=_float(
                "PORTFOLIO_MIN_SELL_VALUE_USD", 2.0
            ),
            auto_trade_floor_percentages=_bool(
                "AUTO_TRADE_FLOOR_PERCENTAGES", False
            ),
            auto_sell_enabled=_bool("AUTO_SELL_ENABLED", False),
            auto_sell_live=_bool("AUTO_SELL_LIVE", False),
            auto_sell_principal_multiple=_float(
                "AUTO_SELL_PRINCIPAL_MULTIPLE", 2.0
            ),
            auto_sell_half_profit_multiple=_float(
                "AUTO_SELL_HALF_PROFIT_MULTIPLE", 3.0
            ),
            auto_sell_second_stage_fraction=_float(
                "AUTO_SELL_SECOND_STAGE_FRACTION", 0.5
            ),
            auto_sell_max_price_impact_pct=_float(
                "AUTO_SELL_MAX_PRICE_IMPACT_PCT", 5.0
            ),
            auto_sell_max_slippage_bps=_int(
                "AUTO_SELL_MAX_SLIPPAGE_BPS", 500
            ),
            emergency_sell_max_price_impact_pct=_float(
                "EMERGENCY_SELL_MAX_PRICE_IMPACT_PCT", 35.0
            ),
            emergency_sell_max_slippage_bps=_int(
                "EMERGENCY_SELL_MAX_SLIPPAGE_BPS", 4000
            ),
            profit_protecting_slippage_margin_bps=_int(
                "PROFIT_PROTECTING_SLIPPAGE_MARGIN_BPS", 200
            ),
            auto_sell_adaptive_chunks=_bool(
                "AUTO_SELL_ADAPTIVE_CHUNKS", False
            ),
            auto_sell_min_chunk_fraction=_float(
                "AUTO_SELL_MIN_CHUNK_FRACTION", 0.01
            ),
            auto_sell_max_chunk_attempts=_int(
                "AUTO_SELL_MAX_CHUNK_ATTEMPTS", 8
            ),
            auto_sell_portfolio_signals=_bool(
                "AUTO_SELL_PORTFOLIO_SIGNALS", False
            ),
            auto_sell_take_partial_fraction=_float(
                "AUTO_SELL_TAKE_PARTIAL_FRACTION", 0.5
            ),
            auto_sell_protect_profit_fraction=_float(
                "AUTO_SELL_PROTECT_PROFIT_FRACTION", 1.0
            ),
            auto_sell_exit_warning_fraction=_float(
                "AUTO_SELL_EXIT_WARNING_FRACTION", 1.0
            ),
            auto_sell_min_value_usd=_float(
                "AUTO_SELL_MIN_VALUE_USD", 2.0
            ),
            auto_sell_signal_confirmation_polls=_int(
                "AUTO_SELL_SIGNAL_CONFIRMATION_POLLS", 3
            ),
            auto_sell_signal_max_gap_seconds=_float(
                "AUTO_SELL_SIGNAL_MAX_GAP_SECONDS", 180.0
            ),
            auto_sell_excluded_mints=tuple(
                dict.fromkeys(
                    (USDC_MINT, *_addresses("AUTO_SELL_EXCLUDED_MINTS"))
                )
            ),
            auto_buy_enabled=_bool("AUTO_BUY_ENABLED", False),
            auto_buy_live=_bool("AUTO_BUY_LIVE", False),
            auto_buy_discovery=_bool("AUTO_BUY_DISCOVERY", False),
            auto_buy_discovery_min_score=_int(
                "AUTO_BUY_DISCOVERY_MIN_SCORE", 70
            ),
            auto_buy_discovery_min_liquidity_usd=_float(
                "AUTO_BUY_DISCOVERY_MIN_LIQUIDITY_USD", 50_000.0
            ),
            auto_buy_discovery_min_price_vs_initial_pct=_float(
                "AUTO_BUY_DISCOVERY_MIN_PRICE_VS_INITIAL_PCT", 20.0
            ),
            auto_buy_discovery_min_price_vs_peak_pct=_float(
                "AUTO_BUY_DISCOVERY_MIN_PRICE_VS_PEAK_PCT", 50.0
            ),
            auto_buy_signal_max_age_seconds=_float(
                "AUTO_BUY_SIGNAL_MAX_AGE_SECONDS", 30.0
            ),
            auto_buy_watch_max_seconds=_float(
                "AUTO_BUY_WATCH_MAX_SECONDS", 86_400.0
            ),
            auto_buy_watch_max_candidates=_int(
                "AUTO_BUY_WATCH_MAX_CANDIDATES", 250
            ),
            auto_buy_watch_batch_size=_int(
                "AUTO_BUY_WATCH_BATCH_SIZE", 10
            ),
            auto_buy_watch_retry_base_seconds=_float(
                "AUTO_BUY_WATCH_RETRY_BASE_SECONDS", 15.0
            ),
            auto_buy_watch_retry_max_seconds=_float(
                "AUTO_BUY_WATCH_RETRY_MAX_SECONDS", 300.0
            ),
            auto_buy_excluded_mints=_addresses(
                "AUTO_BUY_EXCLUDED_MINTS"
            ),
            auto_buy_seed_size_usdc=_float(
                "AUTO_BUY_SEED_SIZE_USDC", 5.0
            ),
            auto_buy_max_seed_buys=_int("AUTO_BUY_MAX_SEED_BUYS", 2),
            auto_buy_max_open_positions=_int(
                "AUTO_BUY_MAX_OPEN_POSITIONS", 2
            ),
            auto_buy_reinvest_profit_pct=_float(
                "AUTO_BUY_REINVEST_PROFIT_PCT", 50.0
            ),
            auto_buy_max_price_impact_pct=_float(
                "AUTO_BUY_MAX_PRICE_IMPACT_PCT", 5.0
            ),
            auto_buy_max_slippage_bps=_int(
                "AUTO_BUY_MAX_SLIPPAGE_BPS", 500
            ),
            momentum_buy_max_price_impact_pct=_float(
                "MOMENTUM_BUY_MAX_PRICE_IMPACT_PCT", 20.0
            ),
            momentum_buy_max_slippage_bps=_int(
                "MOMENTUM_BUY_MAX_SLIPPAGE_BPS", 3000
            ),
            auto_rebuy_enabled=_bool("AUTO_REBUY_ENABLED", False),
            auto_rebuy_cooldown_seconds=_float(
                "AUTO_REBUY_COOLDOWN_SECONDS", 600.0
            ),
            auto_rebuy_max_watch_seconds=_float(
                "AUTO_REBUY_MAX_WATCH_SECONDS", 86_400.0
            ),
            auto_rebuy_min_drop_pct=_float(
                "AUTO_REBUY_MIN_DROP_PCT", 10.0
            ),
            auto_rebuy_min_rebound_pct=_float(
                "AUTO_REBUY_MIN_REBOUND_PCT", 5.0
            ),
            auto_rebuy_min_entry_discount_pct=_float(
                "AUTO_REBUY_MIN_ENTRY_DISCOUNT_PCT", 5.0
            ),
            auto_rebuy_min_momentum_pct=_float(
                "AUTO_REBUY_MIN_MOMENTUM_PCT", 3.0
            ),
            auto_rebuy_min_buy_sell_ratio=_float(
                "AUTO_REBUY_MIN_BUY_SELL_RATIO", 1.4
            ),
            auto_rebuy_min_buys_m5=_int(
                "AUTO_REBUY_MIN_BUYS_M5", 8
            ),
            auto_rebuy_min_liquidity_usd=_float(
                "AUTO_REBUY_MIN_LIQUIDITY_USD", 20_000.0
            ),
            auto_rebuy_min_liquidity_retention_pct=_float(
                "AUTO_REBUY_MIN_LIQUIDITY_RETENTION_PCT", 80.0
            ),
            auto_rebuy_confirmation_polls=_int(
                "AUTO_REBUY_CONFIRMATION_POLLS", 3
            ),
            auto_rebuy_max_per_token=_int(
                "AUTO_REBUY_MAX_PER_TOKEN", 1
            ),
            auto_rebuy_max_size_usdc=_float(
                "AUTO_REBUY_MAX_SIZE_USDC", 5.0
            ),
            jupiter_api_key=os.getenv("JUPITER_API_KEY") or None,
            ethereum_token_addresses=_addresses(
                "ETHEREUM_TOKEN_ADDRESSES"
            ),
            base_token_addresses=_addresses("BASE_TOKEN_ADDRESSES"),
            bnb_token_addresses=_addresses("BNB_TOKEN_ADDRESSES"),
            bob_token_addresses=_addresses("BOB_TOKEN_ADDRESSES"),
            monad_token_addresses=_addresses("MONAD_TOKEN_ADDRESSES"),
            robinhood_token_addresses=_addresses(
                "ROBINHOOD_TOKEN_ADDRESSES"
            ),
            hyperevm_token_addresses=_addresses(
                "HYPEREVM_TOKEN_ADDRESSES"
            ),
            robinhood_poll_seconds=_float("ROBINHOOD_POLL_SECONDS", 15.0),
            multichain_poll_seconds=_float(
                "MULTICHAIN_POLL_SECONDS",
                _float("ROBINHOOD_POLL_SECONDS", 15.0),
            ),
            solana_momentum_poll_seconds=_float(
                "SOLANA_MOMENTUM_POLL_SECONDS", 120.0
            ),
            solana_momentum_throttled_poll_seconds=_float(
                "SOLANA_MOMENTUM_THROTTLED_POLL_SECONDS", 600.0
            ),
            solana_momentum_min_age_days=_float(
                "SOLANA_MOMENTUM_MIN_AGE_DAYS", 3.0
            ),
            bitquery_client_id=(os.getenv("BITQUERY_CLIENT_ID") or None),
            bitquery_client_secret=(os.getenv("BITQUERY_CLIENT_SECRET") or None),
            launchlab_poll_seconds=_float("LAUNCHLAB_POLL_SECONDS", 240.0),
            launchlab_throttled_poll_seconds=_float(
                "LAUNCHLAB_THROTTLED_POLL_SECONDS", 1800.0
            ),
            evm_wallet_address=(os.getenv("EVM_WALLET_ADDRESS") or None),
            hyperliquid_address=(
                os.getenv("HYPERLIQUID_ADDRESS")
                or os.getenv("EVM_WALLET_ADDRESS")
                or None
            ),
            evm_wallet_poll_seconds=_float("EVM_WALLET_POLL_SECONDS", 10.0),
            copyfomo_solana_wallet=(os.getenv("COPYFOMO_SOLANA_WALLET") or None),
            copyfomo_evm_wallet=(os.getenv("COPYFOMO_EVM_WALLET") or None),
            copyfomo_evm_chain=os.getenv("COPYFOMO_EVM_CHAIN", "base"),
            copyfomo_evm_poll_seconds=_float("COPYFOMO_EVM_POLL_SECONDS", 15.0),
            ethereum_rpc_url=os.getenv("ETHEREUM_RPC_URL", ""),
            base_rpc_url=os.getenv("BASE_RPC_URL", ""),
            bnb_rpc_url=os.getenv("BNB_RPC_URL", ""),
            bob_rpc_url=os.getenv("BOB_RPC_URL", ""),
            monad_rpc_url=os.getenv("MONAD_RPC_URL", ""),
            robinhood_rpc_url=os.getenv(
                "ROBINHOOD_RPC_URL",
                "https://rpc.mainnet.chain.robinhood.com",
            ),
            hyperevm_rpc_url=os.getenv(
                "HYPEREVM_RPC_URL", "https://rpc.hyperliquid.xyz/evm"
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.ws_url.startswith(("ws://", "wss://")):
            raise ValueError("PUMPPORTAL_WS_URL must begin with ws:// or wss://")
        if not self.solana_rpc_http_url.startswith(("http://", "https://")):
            raise ValueError("SOLANA_RPC_HTTP_URL must begin with http:// or https://")
        if not self.solana_rpc_ws_url.startswith(("ws://", "wss://")):
            raise ValueError("SOLANA_RPC_WS_URL must begin with ws:// or wss://")
        if self.solana_wallet_address and re.fullmatch(
            r"[1-9A-HJ-NP-Za-km-z]{32,44}", self.solana_wallet_address
        ) is None:
            raise ValueError("SOLANA_WALLET_ADDRESS must be a public Solana address")
        if self.trade_size_sol <= 0:
            raise ValueError("PAPER_TRADE_SIZE_SOL must be greater than zero")
        if self.max_open_positions < 1:
            raise ValueError("MAX_OPEN_POSITIONS must be at least one")
        if self.max_total_exposure_sol < self.trade_size_sol:
            raise ValueError(
                "MAX_TOTAL_EXPOSURE_SOL must be at least PAPER_TRADE_SIZE_SOL"
            )
        if self.max_market_cap_sol < self.min_market_cap_sol:
            raise ValueError(
                "MAX_MARKET_CAP_SOL must be at least MIN_MARKET_CAP_SOL"
            )
        if self.take_profit_pct <= 0 or self.stop_loss_pct <= 0:
            raise ValueError("TAKE_PROFIT_PCT and STOP_LOSS_PCT must be positive")
        if self.price_poll_seconds < 1:
            raise ValueError("PRICE_POLL_SECONDS must be at least one")
        if self.portfolio_poll_seconds < 10:
            raise ValueError("PORTFOLIO_POLL_SECONDS must be at least 10")
        if self.auto_sell_principal_multiple < 2:
            raise ValueError(
                "AUTO_SELL_PRINCIPAL_MULTIPLE must be at least 2"
            )
        if (
            self.auto_sell_half_profit_multiple
            <= self.auto_sell_principal_multiple
        ):
            raise ValueError(
                "AUTO_SELL_HALF_PROFIT_MULTIPLE must exceed the principal level"
            )
        if not 0 < self.auto_sell_second_stage_fraction < 1:
            raise ValueError(
                "AUTO_SELL_SECOND_STAGE_FRACTION must be between 0 and 1"
            )
        if not 0 < self.auto_sell_max_price_impact_pct <= 10:
            raise ValueError(
                "AUTO_SELL_MAX_PRICE_IMPACT_PCT must be above 0 and at most 10"
            )
        if not 1 <= self.auto_sell_max_slippage_bps <= 2_000:
            raise ValueError(
                "AUTO_SELL_MAX_SLIPPAGE_BPS must be from 1 through 2000"
            )
        if not self.auto_sell_max_price_impact_pct < self.emergency_sell_max_price_impact_pct <= 90:
            raise ValueError(
                "EMERGENCY_SELL_MAX_PRICE_IMPACT_PCT must exceed the normal "
                "ceiling and be at most 90"
            )
        if not self.auto_sell_max_slippage_bps < self.emergency_sell_max_slippage_bps <= 9_000:
            raise ValueError(
                "EMERGENCY_SELL_MAX_SLIPPAGE_BPS must exceed the normal "
                "ceiling and be from 1 through 9000"
            )
        if not 0 <= self.profit_protecting_slippage_margin_bps < self.emergency_sell_max_slippage_bps:
            raise ValueError(
                "PROFIT_PROTECTING_SLIPPAGE_MARGIN_BPS must be non-negative "
                "and less than EMERGENCY_SELL_MAX_SLIPPAGE_BPS"
            )
        if not 0 < self.auto_sell_min_chunk_fraction <= 0.25:
            raise ValueError(
                "AUTO_SELL_MIN_CHUNK_FRACTION must be above 0 and at most 0.25"
            )
        if not 1 <= self.auto_sell_max_chunk_attempts <= 12:
            raise ValueError(
                "AUTO_SELL_MAX_CHUNK_ATTEMPTS must be from 1 through 12"
            )
        for name, value in (
            ("AUTO_SELL_TAKE_PARTIAL_FRACTION", self.auto_sell_take_partial_fraction),
            ("AUTO_SELL_PROTECT_PROFIT_FRACTION", self.auto_sell_protect_profit_fraction),
            ("AUTO_SELL_EXIT_WARNING_FRACTION", self.auto_sell_exit_warning_fraction),
        ):
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be above 0 and at most 1")
        if self.auto_sell_min_value_usd <= 0:
            raise ValueError("AUTO_SELL_MIN_VALUE_USD must be above 0")
        if self.portfolio_min_sell_value_usd < 2:
            raise ValueError("PORTFOLIO_MIN_SELL_VALUE_USD must be at least $2")
        if not 1 <= self.auto_sell_signal_confirmation_polls <= 20:
            raise ValueError(
                "AUTO_SELL_SIGNAL_CONFIRMATION_POLLS must be from 1 through 20"
            )
        if self.auto_sell_signal_max_gap_seconds < 10:
            raise ValueError(
                "AUTO_SELL_SIGNAL_MAX_GAP_SECONDS must be at least 10"
            )
        if self.auto_sell_live and not self.auto_sell_enabled:
            raise ValueError("AUTO_SELL_LIVE requires AUTO_SELL_ENABLED=true")
        if self.auto_sell_portfolio_signals and not self.auto_sell_enabled:
            raise ValueError(
                "AUTO_SELL_PORTFOLIO_SIGNALS requires AUTO_SELL_ENABLED=true"
            )
        if self.auto_sell_live and not self.jupiter_api_key:
            raise ValueError("AUTO_SELL_LIVE requires JUPITER_API_KEY")
        if self.auto_sell_live and not self.solana_wallet_address:
            raise ValueError(
                "AUTO_SELL_LIVE requires SOLANA_WALLET_ADDRESS"
            )
        if self.auto_buy_seed_size_usdc < 1:
            raise ValueError("AUTO_BUY_SEED_SIZE_USDC must be at least 1")
        if self.auto_buy_max_seed_buys < 1:
            raise ValueError("AUTO_BUY_MAX_SEED_BUYS must be at least one")
        if self.auto_buy_max_open_positions < 1:
            raise ValueError(
                "AUTO_BUY_MAX_OPEN_POSITIONS must be at least one"
            )
        if not 0 <= self.auto_buy_reinvest_profit_pct <= 100:
            raise ValueError(
                "AUTO_BUY_REINVEST_PROFIT_PCT must be from 0 through 100"
            )
        if not 0 < self.auto_buy_max_price_impact_pct <= 10:
            raise ValueError(
                "AUTO_BUY_MAX_PRICE_IMPACT_PCT must be above 0 and at most 10"
            )
        if not 1 <= self.auto_buy_max_slippage_bps <= 2_000:
            raise ValueError(
                "AUTO_BUY_MAX_SLIPPAGE_BPS must be from 1 through 2000"
            )
        if not self.auto_buy_max_price_impact_pct < self.momentum_buy_max_price_impact_pct <= 50:
            raise ValueError(
                "MOMENTUM_BUY_MAX_PRICE_IMPACT_PCT must exceed the normal "
                "buy ceiling and be at most 50"
            )
        if not self.auto_buy_max_slippage_bps < self.momentum_buy_max_slippage_bps <= 5_000:
            raise ValueError(
                "MOMENTUM_BUY_MAX_SLIPPAGE_BPS must exceed the normal buy "
                "ceiling and be from 1 through 5000"
            )
        if not 0 <= self.auto_buy_discovery_min_score <= 100:
            raise ValueError(
                "AUTO_BUY_DISCOVERY_MIN_SCORE must be from 0 through 100"
            )
        if self.auto_buy_discovery_min_liquidity_usd <= 0:
            raise ValueError(
                "AUTO_BUY_DISCOVERY_MIN_LIQUIDITY_USD must be above 0"
            )
        if not 0 <= self.auto_buy_discovery_min_price_vs_initial_pct <= 100:
            raise ValueError(
                "AUTO_BUY_DISCOVERY_MIN_PRICE_VS_INITIAL_PCT must be from "
                "0 through 100"
            )
        if not 0 <= self.auto_buy_discovery_min_price_vs_peak_pct <= 100:
            raise ValueError(
                "AUTO_BUY_DISCOVERY_MIN_PRICE_VS_PEAK_PCT must be from "
                "0 through 100"
            )
        if self.auto_buy_signal_max_age_seconds < 5:
            raise ValueError(
                "AUTO_BUY_SIGNAL_MAX_AGE_SECONDS must be at least 5"
            )
        if self.auto_buy_watch_max_seconds < 300:
            raise ValueError(
                "AUTO_BUY_WATCH_MAX_SECONDS must be at least 300"
            )
        if not 1 <= self.auto_buy_watch_max_candidates <= 5_000:
            raise ValueError(
                "AUTO_BUY_WATCH_MAX_CANDIDATES must be from 1 through 5000"
            )
        if not 1 <= self.auto_buy_watch_batch_size <= 50:
            raise ValueError(
                "AUTO_BUY_WATCH_BATCH_SIZE must be from 1 through 50"
            )
        if (
            self.auto_buy_watch_batch_size
            > self.auto_buy_watch_max_candidates
        ):
            raise ValueError(
                "AUTO_BUY_WATCH_BATCH_SIZE cannot exceed watch capacity"
            )
        if self.auto_buy_watch_retry_base_seconds < 5:
            raise ValueError(
                "AUTO_BUY_WATCH_RETRY_BASE_SECONDS must be at least 5"
            )
        if (
            self.auto_buy_watch_retry_max_seconds
            < self.auto_buy_watch_retry_base_seconds
        ):
            raise ValueError(
                "AUTO_BUY_WATCH_RETRY_MAX_SECONDS must be at least the base"
            )
        if (
            self.auto_buy_watch_retry_max_seconds
            > self.auto_buy_watch_max_seconds
        ):
            raise ValueError(
                "AUTO_BUY_WATCH_RETRY_MAX_SECONDS cannot exceed watch lifetime"
            )
        if self.auto_buy_live and not self.auto_buy_enabled:
            raise ValueError("AUTO_BUY_LIVE requires AUTO_BUY_ENABLED=true")
        if self.auto_buy_discovery and not self.auto_buy_enabled:
            raise ValueError(
                "AUTO_BUY_DISCOVERY requires AUTO_BUY_ENABLED=true"
            )
        if self.auto_buy_live and not self.auto_sell_live:
            raise ValueError("AUTO_BUY_LIVE requires AUTO_SELL_LIVE=true")
        if self.auto_buy_live and not self.jupiter_api_key:
            raise ValueError("AUTO_BUY_LIVE requires JUPITER_API_KEY")
        if self.auto_buy_live and not self.solana_wallet_address:
            raise ValueError(
                "AUTO_BUY_LIVE requires SOLANA_WALLET_ADDRESS"
            )
        if self.auto_rebuy_enabled and not self.auto_buy_enabled:
            raise ValueError("AUTO_REBUY_ENABLED requires AUTO_BUY_ENABLED=true")
        if self.auto_rebuy_enabled and not self.auto_sell_enabled:
            raise ValueError("AUTO_REBUY_ENABLED requires AUTO_SELL_ENABLED=true")
        if self.auto_rebuy_cooldown_seconds < 60:
            raise ValueError(
                "AUTO_REBUY_COOLDOWN_SECONDS must be at least 60"
            )
        if self.auto_rebuy_max_watch_seconds < self.auto_rebuy_cooldown_seconds:
            raise ValueError(
                "AUTO_REBUY_MAX_WATCH_SECONDS must be at least the cooldown"
            )
        for name, value in (
            ("AUTO_REBUY_MIN_DROP_PCT", self.auto_rebuy_min_drop_pct),
            ("AUTO_REBUY_MIN_REBOUND_PCT", self.auto_rebuy_min_rebound_pct),
            (
                "AUTO_REBUY_MIN_ENTRY_DISCOUNT_PCT",
                self.auto_rebuy_min_entry_discount_pct,
            ),
            (
                "AUTO_REBUY_MIN_LIQUIDITY_RETENTION_PCT",
                self.auto_rebuy_min_liquidity_retention_pct,
            ),
        ):
            if not 0 < value < 100:
                raise ValueError(f"{name} must be above 0 and below 100")
        if self.auto_rebuy_min_momentum_pct <= 0:
            raise ValueError("AUTO_REBUY_MIN_MOMENTUM_PCT must be above 0")
        if self.auto_rebuy_min_buy_sell_ratio <= 0:
            raise ValueError(
                "AUTO_REBUY_MIN_BUY_SELL_RATIO must be above 0"
            )
        if self.auto_rebuy_min_buys_m5 < 1:
            raise ValueError("AUTO_REBUY_MIN_BUYS_M5 must be at least one")
        if self.auto_rebuy_min_liquidity_usd <= 0:
            raise ValueError("AUTO_REBUY_MIN_LIQUIDITY_USD must be above 0")
        if not 1 <= self.auto_rebuy_confirmation_polls <= 20:
            raise ValueError(
                "AUTO_REBUY_CONFIRMATION_POLLS must be from 1 through 20"
            )
        if not 1 <= self.auto_rebuy_max_per_token <= 5:
            raise ValueError(
                "AUTO_REBUY_MAX_PER_TOKEN must be from 1 through 5"
            )
        if not 1 <= self.auto_rebuy_max_size_usdc <= 100:
            raise ValueError(
                "AUTO_REBUY_MAX_SIZE_USDC must be from 1 through 100"
            )
        if self.standard_trade_size_usd <= 0 or self.moonshot_trade_size_usd <= 0:
            raise ValueError("USD position sizes must be greater than zero")
        if self.max_total_exposure_usd < max(
            self.standard_trade_size_usd, self.moonshot_trade_size_usd
        ):
            raise ValueError("MAX_TOTAL_EXPOSURE_USD is too small for one position")
        if not 0 <= self.moonshot_intelligence_score <= 100:
            raise ValueError("MOONSHOT_INTELLIGENCE_SCORE must be 0 through 100")
        if not 0 <= self.core_intelligence_score <= 100:
            raise ValueError("CORE_INTELLIGENCE_SCORE must be 0 through 100")
        if self.core_intelligence_score < self.moonshot_intelligence_score:
            raise ValueError("CORE_INTELLIGENCE_SCORE cannot be lower than moonshot")
        if not 0 < self.trailing_stop_pct < 100:
            raise ValueError("TRAILING_STOP_PCT must be between 0 and 100")
        if not 0 < self.small_gain_trailing_activation_pct < self.trailing_activation_pct:
            raise ValueError(
                "SMALL_GAIN_TRAILING_ACTIVATION_PCT must be positive and "
                "below TRAILING_ACTIVATION_PCT"
            )
        if not 0 < self.small_gain_trailing_stop_pct < 100:
            raise ValueError("SMALL_GAIN_TRAILING_STOP_PCT must be between 0 and 100")
        if not 0 < self.liquidity_drop_pct < 100:
            raise ValueError("LIQUIDITY_DROP_PCT must be between 0 and 100")
        if self.sell_pressure_ratio <= 0 or self.reentry_buy_sell_ratio <= 0:
            raise ValueError("buy/sell ratio settings must be positive")
        if self.max_pending_candidates < 1:
            raise ValueError("MAX_PENDING_CANDIDATES must be at least one")
        if not 1 <= self.recommendation_limit <= 10:
            raise ValueError("RECOMMENDATION_LIMIT must be 1 through 10")
        if self.recommendation_pool_size < self.recommendation_limit:
            raise ValueError(
                "RECOMMENDATION_POOL_SIZE cannot be below RECOMMENDATION_LIMIT"
            )
        if self.recommendation_poll_seconds < 5:
            raise ValueError("RECOMMENDATION_POLL_SECONDS must be at least 5")
        if self.recommendation_ttl_seconds < self.recommendation_poll_seconds:
            raise ValueError(
                "RECOMMENDATION_TTL_SECONDS must be at least the poll interval"
            )
        if not 0 < self.pullback_zone_min_pct < 100:
            raise ValueError("PULLBACK_ZONE_MIN_PCT must be between 0 and 100")
        if not self.pullback_zone_min_pct < self.pullback_zone_max_pct < 100:
            raise ValueError(
                "PULLBACK_ZONE_MAX_PCT must exceed the minimum and be below 100"
            )
        if not 0 < self.pullback_started_pct < self.pullback_zone_min_pct:
            raise ValueError(
                "PULLBACK_STARTED_PCT must be positive and below "
                "PULLBACK_ZONE_MIN_PCT"
            )
        if self.pullback_reclaim_pct <= 0:
            raise ValueError("PULLBACK_RECLAIM_PCT must be positive")
        if self.entry_confirmation_polls < 2:
            raise ValueError("ENTRY_CONFIRMATION_POLLS must be at least 2")
        if not 0 <= self.entry_min_signal_score <= 100:
            raise ValueError("ENTRY_MIN_SIGNAL_SCORE must be 0 through 100")
        if not 0 < self.entry_min_liquidity_retention_pct <= 100:
            raise ValueError(
                "ENTRY_MIN_LIQUIDITY_RETENTION_PCT must be above 0 and at "
                "most 100"
            )
        if self.min_entry_reward_risk_ratio < 1:
            raise ValueError(
                "MIN_ENTRY_REWARD_RISK_RATIO must be at least 1"
            )
        if self.pullback_trigger_pct <= 0:
            raise ValueError("PULLBACK_TRIGGER_PCT must be positive")
        if self.buy_now_min_ratio <= 0:
            raise ValueError("BUY_NOW_MIN_RATIO must be positive")
        if self.momentum_buy_min_ratio <= 0:
            raise ValueError("MOMENTUM_BUY_MIN_RATIO must be positive")
        if self.momentum_buy_min_trades < 0:
            raise ValueError("MOMENTUM_BUY_MIN_TRADES must not be negative")
        if self.momentum_buy_min_liquidity_growth_pct <= 0:
            raise ValueError("MOMENTUM_BUY_MIN_LIQUIDITY_GROWTH_PCT must be positive")
        if self.avoid_entry_momentum_pct >= 0:
            raise ValueError("AVOID_ENTRY_MOMENTUM_PCT must be negative")
        if self.avoid_entry_sell_pressure_ratio <= 0:
            raise ValueError(
                "AVOID_ENTRY_SELL_PRESSURE_RATIO must be positive"
            )
        allowed_alerts = {
            "BUY NOW",
            "BUY ZONE",
            "MOMENTUM BUY",
            "ENTRY PENDING",
            "PULLBACK STARTED",
            "WAIT FOR PULLBACK",
            "WATCH",
            "AVOID",
        }
        invalid_alerts = set(self.pushover_alert_decisions) - allowed_alerts
        if invalid_alerts:
            raise ValueError(
                "PUSHOVER_ALERT_DECISIONS contains unknown states: "
                + ", ".join(sorted(invalid_alerts))
            )
        portfolio_alerts = {
            "BUY MORE",
            "REBOUND WATCH",
            "STRUCTURE WATCH",
            "HOLD",
            "TAKE PARTIAL",
            "PROTECT PROFIT",
            "EXIT WARNING",
        }
        invalid_portfolio_alerts = (
            set(self.pushover_portfolio_alert_decisions) - portfolio_alerts
        )
        if invalid_portfolio_alerts:
            raise ValueError(
                "PUSHOVER_PORTFOLIO_ALERT_DECISIONS contains unknown states: "
                + ", ".join(sorted(invalid_portfolio_alerts))
            )
        all_priority_states = allowed_alerts | portfolio_alerts
        invalid_priority_states = (
            set(self.pushover_high_priority_decisions) - all_priority_states
        )
        if invalid_priority_states:
            raise ValueError(
                "PUSHOVER_HIGH_PRIORITY_DECISIONS contains unknown states: "
                + ", ".join(sorted(invalid_priority_states))
            )
        if not 0 <= self.pushover_min_score <= 100:
            raise ValueError("PUSHOVER_MIN_SCORE must be 0 through 100")
        if self.pushover_cooldown_seconds < 15:
            raise ValueError("PUSHOVER_COOLDOWN_SECONDS must be at least 15")
        if self.pushover_portfolio_cooldown_seconds < 15:
            raise ValueError(
                "PUSHOVER_PORTFOLIO_COOLDOWN_SECONDS must be at least 15"
            )
        if self.pushover_enabled:
            credential_pattern = r"[A-Za-z0-9]{30}"
            if not self.pushover_app_token or re.fullmatch(
                credential_pattern, self.pushover_app_token
            ) is None:
                raise ValueError(
                    "PUSHOVER_APP_TOKEN must be a 30-character application token"
                )
            if not self.pushover_user_key or re.fullmatch(
                credential_pattern, self.pushover_user_key
            ) is None:
                raise ValueError(
                    "PUSHOVER_USER_KEY must be a 30-character user key"
                )
            if self.pushover_device and re.fullmatch(
                r"[A-Za-z0-9_-]{1,25}", self.pushover_device
            ) is None:
                raise ValueError("PUSHOVER_DEVICE contains invalid characters")
        if self.robinhood_poll_seconds < 15:
            raise ValueError("ROBINHOOD_POLL_SECONDS must be at least 15")
        if self.multichain_poll_seconds < 15:
            raise ValueError("MULTICHAIN_POLL_SECONDS must be at least 15")
        if self.evm_wallet_poll_seconds < 5:
            raise ValueError("EVM_WALLET_POLL_SECONDS must be at least 5")
        evm_address_pattern = r"0x[0-9a-fA-F]{40}"
        for address in (self.evm_wallet_address, self.hyperliquid_address, self.copyfomo_evm_wallet):
            if address and re.fullmatch(evm_address_pattern, address) is None:
                raise ValueError(f"invalid EVM public address: {address}")
        if self.copyfomo_solana_wallet and not 32 <= len(self.copyfomo_solana_wallet) <= 44:
            raise ValueError(f"invalid COPYFOMO_SOLANA_WALLET: {self.copyfomo_solana_wallet}")
        if self.copyfomo_evm_poll_seconds < 5:
            raise ValueError("COPYFOMO_EVM_POLL_SECONDS must be at least 5")
        token_groups = (
            self.ethereum_token_addresses,
            self.base_token_addresses,
            self.bnb_token_addresses,
            self.bob_token_addresses,
            self.monad_token_addresses,
            self.robinhood_token_addresses,
            self.hyperevm_token_addresses,
        )
        for address in (item for group in token_groups for item in group):
            if re.fullmatch(evm_address_pattern, address) is None:
                raise ValueError(
                    "a chain TOKEN_ADDRESSES setting contains an invalid EVM "
                    f"contract: {address}"
                )
        for name, url in self.evm_rpc_urls.items():
            if url and not url.startswith(("http://", "https://")):
                raise ValueError(f"{name} RPC URL must begin with http:// or https://")
        for wallet in self.watched_wallets:
            if not 32 <= len(wallet) <= 44:
                raise ValueError(f"WATCHED_WALLETS contains an invalid address: {wallet}")

    @property
    def websocket_uri(self) -> str:
        if not self.api_key:
            return self.ws_url
        parts = urlsplit(self.ws_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["api-key"] = self.api_key
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )

    @property
    def evm_rpc_urls(self) -> dict[str, str]:
        return {
            "ethereum": self.ethereum_rpc_url,
            "base": self.base_rpc_url,
            "bsc": self.bnb_rpc_url,
            "bob": self.bob_rpc_url,
            "monad": self.monad_rpc_url,
            "robinhood": self.robinhood_rpc_url,
            "hyperevm": self.hyperevm_rpc_url,
        }

    @property
    def multichain_token_addresses(self) -> dict[str, tuple[str, ...]]:
        return {
            "ethereum": self.ethereum_token_addresses,
            "base": self.base_token_addresses,
            "bsc": self.bnb_token_addresses,
            "bob": self.bob_token_addresses,
            "monad": self.monad_token_addresses,
            "robinhood": self.robinhood_token_addresses,
            "hyperevm": self.hyperevm_token_addresses,
        }
