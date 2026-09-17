from __future__ import annotations

import os
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
    watched_wallets: tuple[str, ...] = ()
    price_poll_seconds: float = 5.0
    copy_min_liquidity_usd: float = 10_000.0

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
            watched_wallets=_wallets("WATCHED_WALLETS"),
            price_poll_seconds=_float("PRICE_POLL_SECONDS", 5.0),
            copy_min_liquidity_usd=_float("COPY_MIN_LIQUIDITY_USD", 10_000.0),
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
