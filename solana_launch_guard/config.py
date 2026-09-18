from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


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
    entry_confirmation_polls: int = 3
    entry_min_signal_score: int = 65
    entry_min_liquidity_retention_pct: float = 80.0
    entry_require_nonfalling_volume: bool = True
    min_entry_reward_risk_ratio: float = 2.0
    buy_now_min_ratio: float = 1.2
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
    color_output: bool = True
    recommendation_snapshot_path: str = "launch_guard_recommendations.json"
    portfolio_snapshot_path: str = "launch_guard_portfolio.json"
    portfolio_poll_seconds: float = 15.0
    portfolio_min_value_usd: float = 0.01
    ethereum_token_addresses: tuple[str, ...] = ()
    base_token_addresses: tuple[str, ...] = ()
    bnb_token_addresses: tuple[str, ...] = ()
    bob_token_addresses: tuple[str, ...] = ()
    monad_token_addresses: tuple[str, ...] = ()
    robinhood_token_addresses: tuple[str, ...] = ()
    hyperevm_token_addresses: tuple[str, ...] = ()
    robinhood_poll_seconds: float = 15.0
    multichain_poll_seconds: float = 15.0
    evm_wallet_address: str | None = None
    hyperliquid_address: str | None = None
    evm_wallet_poll_seconds: float = 10.0
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
            entry_confirmation_polls=_int("ENTRY_CONFIRMATION_POLLS", 3),
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
            color_output=_bool("COLOR_OUTPUT", True),
            recommendation_snapshot_path=os.getenv(
                "RECOMMENDATION_SNAPSHOT_PATH",
                "launch_guard_recommendations.json",
            ),
            portfolio_snapshot_path=os.getenv(
                "PORTFOLIO_SNAPSHOT_PATH", "launch_guard_portfolio.json"
            ),
            portfolio_poll_seconds=_float("PORTFOLIO_POLL_SECONDS", 15.0),
            portfolio_min_value_usd=_float(
                "PORTFOLIO_MIN_VALUE_USD", 0.01
            ),
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
            evm_wallet_address=(os.getenv("EVM_WALLET_ADDRESS") or None),
            hyperliquid_address=(
                os.getenv("HYPERLIQUID_ADDRESS")
                or os.getenv("EVM_WALLET_ADDRESS")
                or None
            ),
            evm_wallet_poll_seconds=_float("EVM_WALLET_POLL_SECONDS", 10.0),
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
        if self.avoid_entry_momentum_pct >= 0:
            raise ValueError("AVOID_ENTRY_MOMENTUM_PCT must be negative")
        if self.avoid_entry_sell_pressure_ratio <= 0:
            raise ValueError(
                "AVOID_ENTRY_SELL_PRESSURE_RATIO must be positive"
            )
        allowed_alerts = {
            "BUY NOW",
            "BUY ZONE",
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
        if not 0 <= self.pushover_min_score <= 100:
            raise ValueError("PUSHOVER_MIN_SCORE must be 0 through 100")
        if self.pushover_cooldown_seconds < 15:
            raise ValueError("PUSHOVER_COOLDOWN_SECONDS must be at least 15")
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
        for address in (self.evm_wallet_address, self.hyperliquid_address):
            if address and re.fullmatch(evm_address_pattern, address) is None:
                raise ValueError(f"invalid EVM public address: {address}")
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
