from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from .config import Settings
from .portfolio import OwnedHolding


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# See SQLiteStore.reconcile_stale_auto_buy_positions's docstring: a mint
# must look unheld for this many consecutive calls before that method's
# irreversible write fires, so one transient partial wallet-balance read
# can't permanently wipe bookkeeping for a position that was never sold.
AUTO_BUY_RECONCILE_MISSING_STREAK = 3


def optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def indicative_price(payload: Mapping[str, Any]) -> float | None:
    """Return SOL per token from virtual reserves when both values are usable."""
    virtual_sol = optional_float(payload.get("vSolInBondingCurve"))
    virtual_tokens = optional_float(payload.get("vTokensInBondingCurve"))
    if virtual_sol is None or virtual_tokens is None or virtual_tokens <= 0:
        return None
    return virtual_sol / virtual_tokens


@dataclass(frozen=True, slots=True)
class Launch:
    mint: str
    name: str
    symbol: str
    creator: str | None
    signature: str | None
    virtual_sol: float | None
    virtual_tokens: float | None
    market_cap_sol: float | None
    creator_buy_sol: float | None
    price_sol: float | None
    received_at: str
    raw: Mapping[str, Any] = field(repr=False)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Launch":
        mint = str(payload.get("mint") or "").strip()
        if not mint:
            raise ValueError("new-token event is missing mint")
        return cls(
            mint=mint,
            name=str(payload.get("name") or "Unknown"),
            symbol=str(payload.get("symbol") or "UNKNOWN"),
            creator=(
                str(payload.get("traderPublicKey"))
                if payload.get("traderPublicKey")
                else None
            ),
            signature=(
                str(payload.get("signature")) if payload.get("signature") else None
            ),
            virtual_sol=optional_float(payload.get("vSolInBondingCurve")),
            virtual_tokens=optional_float(payload.get("vTokensInBondingCurve")),
            market_cap_sol=optional_float(payload.get("marketCapSol")),
            creator_buy_sol=optional_float(payload.get("solAmount")),
            price_sol=indicative_price(payload),
            received_at=utc_now(),
            raw=dict(payload),
        )


@dataclass(frozen=True, slots=True)
class RiskDecision:
    accepted: bool
    score: int
    reasons: tuple[str, ...]


class PortfolioView(Protocol):
    @property
    def open_count(self) -> int: ...

    @property
    def exposure_sol(self) -> float: ...

    def has_position(self, mint: str) -> bool: ...


class RiskEngine:
    """Deterministic gates based only on values present in the launch event."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(self, launch: Launch, portfolio: PortfolioView) -> RiskDecision:
        return self._evaluate(launch, portfolio)

    def evaluate_candidate(self, launch: Launch) -> RiskDecision:
        """Apply launch-data gates without portfolio-capacity restrictions."""
        return self._evaluate(launch, None)

    def _evaluate(
        self, launch: Launch, portfolio: PortfolioView | None
    ) -> RiskDecision:
        reasons: list[str] = []
        deductions = 0

        if portfolio is not None:
            if portfolio.has_position(launch.mint):
                reasons.append("position already exists for mint")

            if portfolio.open_count >= self.settings.max_open_positions:
                reasons.append("maximum open positions reached")

            projected = portfolio.exposure_sol + self.settings.trade_size_sol
            if projected > self.settings.max_total_exposure_sol + 1e-12:
                reasons.append("maximum total exposure would be exceeded")

        if launch.price_sol is None:
            deductions += 35
            if self.settings.reject_unknown_price:
                reasons.append("price unavailable from virtual reserves")

        if launch.virtual_sol is None:
            deductions += 20
            reasons.append("virtual SOL reserve unavailable")
        elif launch.virtual_sol < self.settings.min_virtual_sol:
            reasons.append(
                f"virtual SOL {launch.virtual_sol:.6g} below "
                f"minimum {self.settings.min_virtual_sol:.6g}"
            )

        if launch.market_cap_sol is None:
            deductions += 15
            reasons.append("market cap unavailable")
        elif launch.market_cap_sol < self.settings.min_market_cap_sol:
            reasons.append(
                f"market cap {launch.market_cap_sol:.6g} SOL below "
                f"minimum {self.settings.min_market_cap_sol:.6g}"
            )
        elif launch.market_cap_sol > self.settings.max_market_cap_sol:
            reasons.append(
                f"market cap {launch.market_cap_sol:.6g} SOL above "
                f"maximum {self.settings.max_market_cap_sol:.6g}"
            )

        if launch.creator_buy_sol is None:
            deductions += 10
        elif launch.creator_buy_sol > self.settings.max_creator_buy_sol:
            reasons.append(
                f"creator buy {launch.creator_buy_sol:.6g} SOL above "
                f"maximum {self.settings.max_creator_buy_sol:.6g}"
            )

        # Event-level data cannot establish these facts. Keep the score below 100
        # so callers cannot mistake an accepted event for a verified-safe token.
        deductions += 20
        score = max(0, 100 - deductions - min(40, 10 * len(reasons)))
        return RiskDecision(not reasons, score, tuple(reasons))


@dataclass(slots=True)
class Position:
    mint: str
    symbol: str
    entry_price_sol: float
    quantity: float
    cost_sol: float
    take_profit_price_sol: float
    stop_loss_price_sol: float
    opened_at: str
    latest_price_sol: float
    status: str = "OPEN"
    closed_at: str | None = None
    exit_price_sol: float | None = None
    exit_reason: str | None = None
    pnl_sol: float | None = None
    pnl_pct: float | None = None

    def mark(self, price_sol: float) -> str | None:
        if self.status != "OPEN" or price_sol <= 0:
            return None

        self.latest_price_sol = price_sol
        if price_sol >= self.take_profit_price_sol:
            self.close(price_sol, "TAKE_PROFIT")
            return self.exit_reason
        if price_sol <= self.stop_loss_price_sol:
            self.close(price_sol, "STOP_LOSS")
            return self.exit_reason
        return None

    def close(self, price_sol: float, reason: str) -> None:
        proceeds = self.quantity * price_sol
        self.status = "CLOSED"
        self.closed_at = utc_now()
        self.exit_price_sol = price_sol
        self.exit_reason = reason
        self.pnl_sol = proceeds - self.cost_sol
        self.pnl_pct = (price_sol / self.entry_price_sol - 1.0) * 100.0


class PaperBroker:
    def __init__(self, settings: Settings, store: "SQLiteStore") -> None:
        self.settings = settings
        self.store = store
        self.positions: dict[str, Position] = {
            position.mint: position for position in store.load_open_positions()
        }

    @property
    def open_count(self) -> int:
        return sum(position.status == "OPEN" for position in self.positions.values())

    @property
    def exposure_sol(self) -> float:
        return sum(
            position.cost_sol
            for position in self.positions.values()
            if position.status == "OPEN"
        )

    def has_position(self, mint: str) -> bool:
        position = self.positions.get(mint)
        return position is not None and position.status == "OPEN"

    def open(
        self,
        launch: Launch,
        reason: str = "RISK_PASS",
        *,
        cost_sol: float | None = None,
        take_profit_pct: float | None = None,
        stop_loss_pct: float | None = None,
    ) -> Position:
        if launch.price_sol is None or launch.price_sol <= 0:
            raise ValueError("cannot open a paper position without a positive price")
        if self.has_position(launch.mint):
            raise ValueError("a paper position already exists for this mint")

        cost = self.settings.trade_size_sol if cost_sol is None else cost_sol
        if cost <= 0:
            raise ValueError("paper position cost must be positive")
        tp_pct = (
            self.settings.take_profit_pct
            if take_profit_pct is None
            else take_profit_pct
        )
        sl_pct = (
            self.settings.stop_loss_pct
            if stop_loss_pct is None
            else stop_loss_pct
        )
        position = Position(
            mint=launch.mint,
            symbol=launch.symbol,
            entry_price_sol=launch.price_sol,
            quantity=cost / launch.price_sol,
            cost_sol=cost,
            take_profit_price_sol=(
                launch.price_sol * (1 + tp_pct / 100)
            ),
            stop_loss_price_sol=(
                launch.price_sol * (1 - sl_pct / 100)
            ),
            opened_at=utc_now(),
            latest_price_sol=launch.price_sol,
        )
        self.positions[launch.mint] = position
        self.store.save_position(position)
        self.store.save_fill(
            mint=position.mint,
            side="BUY",
            price_sol=position.entry_price_sol,
            quantity=position.quantity,
            amount_sol=position.cost_sol,
            reason=reason,
        )
        return position

    def close(self, mint: str, price_sol: float, reason: str) -> Position | None:
        position = self.positions.get(mint)
        if position is None or position.status != "OPEN":
            return None
        position.close(price_sol, reason)
        self.store.save_position(position)
        self.store.save_fill(
            mint=position.mint,
            side="SELL",
            price_sol=price_sol,
            quantity=position.quantity,
            amount_sol=position.quantity * price_sol,
            reason=reason,
        )
        return position

    def mark(self, mint: str, price_sol: float) -> Position | None:
        position = self.positions.get(mint)
        if position is None or position.status != "OPEN":
            return None

        exit_reason = position.mark(price_sol)
        self.store.save_position(position)
        if exit_reason:
            self.store.save_fill(
                mint=position.mint,
                side="SELL",
                price_sol=price_sol,
                quantity=position.quantity,
                amount_sol=position.quantity * price_sol,
                reason=exit_reason,
            )
        return position


class SQLiteStore:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self._migrate()
        # In-memory only, per process lifetime - see
        # reconcile_stale_auto_buy_positions's grace-period docstring.
        self._auto_buy_missing_streaks: dict[str, int] = {}

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            PRAGMA journal_mode = WAL;

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TEXT NOT NULL,
                event_kind TEXT NOT NULL,
                mint TEXT,
                payload_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decided_at TEXT NOT NULL,
                mint TEXT NOT NULL,
                symbol TEXT NOT NULL,
                accepted INTEGER NOT NULL,
                score INTEGER NOT NULL,
                reasons_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS positions (
                mint TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                entry_price_sol REAL NOT NULL,
                latest_price_sol REAL NOT NULL,
                quantity REAL NOT NULL,
                cost_sol REAL NOT NULL,
                take_profit_price_sol REAL NOT NULL,
                stop_loss_price_sol REAL NOT NULL,
                opened_at TEXT NOT NULL,
                status TEXT NOT NULL,
                closed_at TEXT,
                exit_price_sol REAL,
                exit_reason TEXT,
                pnl_sol REAL,
                pnl_pct REAL
            );

            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filled_at TEXT NOT NULL,
                mint TEXT NOT NULL,
                side TEXT NOT NULL,
                price_sol REAL NOT NULL,
                quantity REAL NOT NULL,
                amount_sol REAL NOT NULL,
                reason TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS intelligence_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scored_at TEXT NOT NULL,
                mint TEXT NOT NULL,
                symbol TEXT NOT NULL,
                tier TEXT NOT NULL,
                total_score INTEGER NOT NULL,
                safety_score INTEGER NOT NULL,
                momentum_score INTEGER NOT NULL,
                reasons_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS buy_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signaled_at TEXT NOT NULL,
                mint TEXT NOT NULL,
                symbol TEXT NOT NULL,
                chain TEXT NOT NULL,
                decision TEXT NOT NULL,
                price REAL,
                price_currency TEXT,
                liquidity_usd REAL,
                signal_score INTEGER,
                pair_created_at_ms INTEGER,
                reason TEXT,
                live_blocked_reason TEXT,
                candle_pattern TEXT,
                candle_trend TEXT
            );

            CREATE TABLE IF NOT EXISTS wallet_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seen_at TEXT NOT NULL,
                wallet TEXT NOT NULL,
                signature TEXT NOT NULL,
                slot INTEGER NOT NULL,
                mint TEXT NOT NULL,
                symbol TEXT,
                side TEXT NOT NULL,
                token_delta REAL NOT NULL,
                native_sol_delta REAL,
                observed_price_sol REAL,
                UNIQUE(wallet, signature, mint)
            );

            CREATE TABLE IF NOT EXISTS wallet_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seen_at TEXT NOT NULL,
                chain TEXT NOT NULL,
                wallet TEXT NOT NULL,
                event_id TEXT NOT NULL,
                block_number INTEGER,
                token_address TEXT NOT NULL,
                symbol TEXT,
                direction TEXT NOT NULL,
                token_amount REAL NOT NULL,
                price_usd REAL,
                source TEXT NOT NULL,
                UNIQUE(chain, wallet, event_id, token_address, direction)
            );

            CREATE TABLE IF NOT EXISTS notification_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_at REAL NOT NULL,
                provider TEXT NOT NULL,
                candidate_key TEXT NOT NULL,
                symbol TEXT NOT NULL,
                decision TEXT NOT NULL,
                request_id TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_notification_candidate
            ON notification_events(provider, candidate_key, id DESC);

            CREATE TABLE IF NOT EXISTS owned_holdings (
                chain TEXT NOT NULL,
                token_address TEXT NOT NULL,
                symbol TEXT NOT NULL,
                quantity REAL NOT NULL,
                entry_price REAL,
                price_currency TEXT,
                cost_amount REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chain, token_address)
            );

            CREATE TABLE IF NOT EXISTS portfolio_states (
                chain TEXT NOT NULL,
                token_address TEXT NOT NULL,
                peak_price REAL NOT NULL,
                baseline_liquidity_usd REAL NOT NULL,
                below_sell_minimum INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chain, token_address)
            );

            CREATE TABLE IF NOT EXISTS external_wallet_classifications (
                token_address TEXT PRIMARY KEY,
                first_seen_value_usd REAL NOT NULL,
                classification TEXT NOT NULL,
                classified_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS loss_sale_reviews (
                sale_id TEXT PRIMARY KEY,
                chain TEXT NOT NULL,
                token_address TEXT NOT NULL,
                symbol TEXT NOT NULL,
                source TEXT NOT NULL,
                cost_usd REAL NOT NULL,
                proceeds_usd REAL NOT NULL,
                quantity REAL NOT NULL,
                sold_at_epoch REAL NOT NULL,
                exit_liquidity_usd REAL,
                lowest_price_usd REAL NOT NULL,
                last_price_usd REAL,
                confirmation_count INTEGER NOT NULL DEFAULT 0,
                decision TEXT NOT NULL DEFAULT 'REBUY WATCH',
                reason TEXT NOT NULL DEFAULT 'waiting for market evidence',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auto_sell_policies (
                chain TEXT NOT NULL,
                token_address TEXT NOT NULL,
                armed INTEGER NOT NULL DEFAULT 0,
                stage INTEGER NOT NULL DEFAULT 0,
                last_signature TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chain, token_address)
            );

            CREATE TABLE IF NOT EXISTS auto_sell_executions (
                event_key TEXT PRIMARY KEY,
                chain TEXT NOT NULL,
                token_address TEXT NOT NULL,
                symbol TEXT NOT NULL,
                stage INTEGER NOT NULL,
                status TEXT NOT NULL,
                requested_raw INTEGER NOT NULL,
                balance_before_raw INTEGER,
                expected_output_raw INTEGER,
                signature TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auto_sell_batches (
                batch_key TEXT PRIMARY KEY,
                chain TEXT NOT NULL,
                token_address TEXT NOT NULL,
                symbol TEXT NOT NULL,
                stage INTEGER NOT NULL,
                status TEXT NOT NULL,
                target_raw INTEGER NOT NULL,
                full_exit INTEGER NOT NULL DEFAULT 0,
                sold_raw INTEGER NOT NULL DEFAULT 0,
                proceeds_usdc_raw INTEGER NOT NULL DEFAULT 0,
                next_chunk_index INTEGER NOT NULL DEFAULT 0,
                last_signature TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auto_sell_signal_confirmations (
                chain TEXT NOT NULL,
                token_address TEXT NOT NULL,
                decision TEXT NOT NULL,
                consecutive_polls INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL,
                last_seen_epoch REAL NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chain, token_address)
            );

            CREATE TABLE IF NOT EXISTS auto_buy_policies (
                chain TEXT NOT NULL,
                token_address TEXT NOT NULL,
                symbol TEXT NOT NULL,
                armed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chain, token_address)
            );

            CREATE TABLE IF NOT EXISTS auto_buy_executions (
                event_key TEXT PRIMARY KEY,
                chain TEXT NOT NULL,
                token_address TEXT NOT NULL,
                symbol TEXT NOT NULL,
                funding_source TEXT NOT NULL,
                status TEXT NOT NULL,
                input_usdc_raw INTEGER NOT NULL,
                expected_output_raw INTEGER,
                actual_output_raw INTEGER,
                output_decimals INTEGER,
                signature TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auto_buy_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chain TEXT NOT NULL,
                token_address TEXT NOT NULL,
                symbol TEXT NOT NULL,
                status TEXT NOT NULL,
                cost_usdc_raw INTEGER NOT NULL,
                token_amount_raw INTEGER NOT NULL,
                remaining_raw INTEGER NOT NULL,
                token_decimals INTEGER NOT NULL,
                realized_proceeds_usdc_raw INTEGER NOT NULL DEFAULT 0,
                realized_profit_usdc_raw INTEGER NOT NULL DEFAULT 0,
                reinvest_credit_usdc_raw INTEGER NOT NULL DEFAULT 0,
                buy_signature TEXT NOT NULL UNIQUE,
                opened_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auto_buy_fund (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                seed_buys_used INTEGER NOT NULL DEFAULT 0,
                reinvest_available_usdc_raw INTEGER NOT NULL DEFAULT 0,
                realized_profit_usdc_raw INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auto_buy_discovery_watches (
                chain TEXT NOT NULL,
                token_address TEXT NOT NULL,
                symbol TEXT NOT NULL,
                status TEXT NOT NULL,
                launch_json TEXT NOT NULL,
                candidate_json TEXT,
                tier TEXT,
                intelligence_score INTEGER NOT NULL DEFAULT 0,
                signal_score INTEGER NOT NULL DEFAULT 0,
                decision TEXT,
                liquidity_usd REAL,
                attempts INTEGER NOT NULL DEFAULT 0,
                quote_failures INTEGER NOT NULL DEFAULT 0,
                first_seen_epoch REAL NOT NULL,
                last_seen_epoch REAL,
                next_check_epoch REAL NOT NULL,
                expires_at_epoch REAL NOT NULL,
                last_reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chain, token_address)
            );

            CREATE INDEX IF NOT EXISTS idx_auto_buy_discovery_due
            ON auto_buy_discovery_watches(status, next_check_epoch);

            CREATE TABLE IF NOT EXISTS auto_rebuy_watches (
                chain TEXT NOT NULL,
                token_address TEXT NOT NULL,
                symbol TEXT NOT NULL,
                status TEXT NOT NULL,
                completed_rebuys INTEGER NOT NULL DEFAULT 0,
                cycle INTEGER NOT NULL DEFAULT 1,
                sell_signature TEXT NOT NULL,
                exit_price_usd REAL NOT NULL,
                exit_liquidity_usd REAL,
                lowest_price_usd REAL NOT NULL,
                last_price_usd REAL,
                confirmation_count INTEGER NOT NULL DEFAULT 0,
                sale_proceeds_usdc_raw INTEGER NOT NULL,
                sold_at_epoch REAL NOT NULL,
                last_seen_at_epoch REAL,
                buy_signature TEXT,
                last_reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chain, token_address)
            );

            INSERT OR IGNORE INTO auto_buy_fund(
                id, seed_buys_used, reinvest_available_usdc_raw,
                realized_profit_usdc_raw, updated_at
            ) VALUES (1, 0, 0, 0, '');

            INSERT OR IGNORE INTO owned_holdings(
                chain, token_address, symbol, quantity, entry_price,
                price_currency, cost_amount, updated_at
            )
            SELECT
                'solana', p.mint, p.symbol, p.quantity, p.entry_price_sol,
                'SOL', p.cost_sol, p.opened_at
            FROM positions p
            WHERE EXISTS (
                SELECT 1 FROM fills f
                WHERE f.mint = p.mint AND f.reason = 'FOMO_MANUAL_IMPORT'
            );
            """
        )
        execution_columns = {
            str(row["name"])
            for row in self.connection.execute(
                "PRAGMA table_info(auto_sell_executions)"
            ).fetchall()
        }
        if "balance_before_raw" not in execution_columns:
            self.connection.execute(
                "ALTER TABLE auto_sell_executions "
                "ADD COLUMN balance_before_raw INTEGER"
            )
        batch_columns = {
            str(row["name"])
            for row in self.connection.execute(
                "PRAGMA table_info(auto_sell_batches)"
            ).fetchall()
        }
        if "proceeds_usdc_raw" not in batch_columns:
            self.connection.execute(
                "ALTER TABLE auto_sell_batches "
                "ADD COLUMN proceeds_usdc_raw INTEGER NOT NULL DEFAULT 0"
            )
        signal_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(buy_signals)")
        }
        for column in ("candle_pattern", "candle_trend"):
            if column not in signal_columns:
                self.connection.execute(
                    f"ALTER TABLE buy_signals ADD COLUMN {column} TEXT"
                )
        portfolio_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(portfolio_states)")
        }
        if "below_sell_minimum" not in portfolio_columns:
            self.connection.execute(
                "ALTER TABLE portfolio_states "
                "ADD COLUMN below_sell_minimum INTEGER NOT NULL DEFAULT 0"
            )
        self.connection.commit()

    def save_event(
        self, event_kind: str, payload: Mapping[str, Any], mint: str | None = None
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO events(received_at, event_kind, mint, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                utc_now(),
                event_kind,
                mint,
                json.dumps(dict(payload), separators=(",", ":"), default=str),
            ),
        )
        self.connection.commit()

    def save_decision(self, launch: Launch, decision: RiskDecision) -> None:
        self.connection.execute(
            """
            INSERT INTO decisions(
                decided_at, mint, symbol, accepted, score, reasons_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                launch.mint,
                launch.symbol,
                int(decision.accepted),
                decision.score,
                json.dumps(decision.reasons),
            ),
        )
        self.connection.commit()

    def save_position(self, position: Position) -> None:
        self.connection.execute(
            """
            INSERT INTO positions(
                mint, symbol, entry_price_sol, latest_price_sol, quantity,
                cost_sol, take_profit_price_sol, stop_loss_price_sol,
                opened_at, status, closed_at, exit_price_sol, exit_reason,
                pnl_sol, pnl_pct
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mint) DO UPDATE SET
                symbol=excluded.symbol,
                entry_price_sol=excluded.entry_price_sol,
                latest_price_sol=excluded.latest_price_sol,
                quantity=excluded.quantity,
                cost_sol=excluded.cost_sol,
                take_profit_price_sol=excluded.take_profit_price_sol,
                stop_loss_price_sol=excluded.stop_loss_price_sol,
                opened_at=excluded.opened_at,
                status=excluded.status,
                closed_at=excluded.closed_at,
                exit_price_sol=excluded.exit_price_sol,
                exit_reason=excluded.exit_reason,
                pnl_sol=excluded.pnl_sol,
                pnl_pct=excluded.pnl_pct
            """,
            (
                position.mint,
                position.symbol,
                position.entry_price_sol,
                position.latest_price_sol,
                position.quantity,
                position.cost_sol,
                position.take_profit_price_sol,
                position.stop_loss_price_sol,
                position.opened_at,
                position.status,
                position.closed_at,
                position.exit_price_sol,
                position.exit_reason,
                position.pnl_sol,
                position.pnl_pct,
            ),
        )
        self.connection.commit()

    def save_fill(
        self,
        *,
        mint: str,
        side: str,
        price_sol: float,
        quantity: float,
        amount_sol: float,
        reason: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO fills(
                filled_at, mint, side, price_sol, quantity, amount_sol, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (utc_now(), mint, side, price_sol, quantity, amount_sol, reason),
        )
        self.connection.commit()

    def save_buy_signal(
        self,
        *,
        mint: str,
        symbol: str,
        chain: str,
        decision: str,
        price: float | None,
        price_currency: str | None,
        liquidity_usd: float | None,
        signal_score: int | None,
        pair_created_at_ms: int | None,
        reason: str | None,
        live_blocked_reason: str | None,
    ) -> int:
        """One row each time a candidate moves into a buy decision. Written
        for evaluation only; nothing on a trading path reads it."""
        cursor = self.connection.execute(
            """
            INSERT INTO buy_signals(
                signaled_at, mint, symbol, chain, decision, price,
                price_currency, liquidity_usd, signal_score,
                pair_created_at_ms, reason, live_blocked_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(), mint, symbol, chain, decision, price, price_currency,
                liquidity_usd, signal_score, pair_created_at_ms, reason,
                live_blocked_reason,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid or 0)

    def tag_buy_signal(self, signal_id: int, pattern: str, trend: str | None) -> None:
        """Candle shape at the moment a signal fired (research only)."""
        self.connection.execute(
            "UPDATE buy_signals SET candle_pattern = ?, candle_trend = ? WHERE id = ?",
            (pattern, trend, signal_id),
        )
        self.connection.commit()

    def save_intelligence_score(
        self,
        *,
        mint: str,
        symbol: str,
        tier: str,
        total_score: int,
        safety_score: int,
        momentum_score: int,
        reasons: tuple[str, ...],
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO intelligence_scores(
                scored_at, mint, symbol, tier, total_score,
                safety_score, momentum_score, reasons_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                mint,
                symbol,
                tier,
                total_score,
                safety_score,
                momentum_score,
                json.dumps(reasons),
            ),
        )
        self.connection.commit()

    def load_open_positions(self) -> list[Position]:
        rows = self.connection.execute(
            "SELECT * FROM positions WHERE status = 'OPEN'"
        ).fetchall()
        return [
            Position(
                mint=row["mint"],
                symbol=row["symbol"],
                entry_price_sol=float(row["entry_price_sol"]),
                latest_price_sol=float(row["latest_price_sol"]),
                quantity=float(row["quantity"]),
                cost_sol=float(row["cost_sol"]),
                take_profit_price_sol=float(row["take_profit_price_sol"]),
                stop_loss_price_sol=float(row["stop_loss_price_sol"]),
                opened_at=row["opened_at"],
                status=row["status"],
                closed_at=row["closed_at"],
                exit_price_sol=row["exit_price_sol"],
                exit_reason=row["exit_reason"],
                pnl_sol=row["pnl_sol"],
                pnl_pct=row["pnl_pct"],
            )
            for row in rows
        ]

    def save_owned_holding(self, holding: OwnedHolding) -> None:
        self.connection.execute(
            """
            INSERT INTO owned_holdings(
                chain, token_address, symbol, quantity, entry_price,
                price_currency, cost_amount, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chain, token_address) DO UPDATE SET
                symbol = excluded.symbol,
                quantity = excluded.quantity,
                entry_price = excluded.entry_price,
                price_currency = excluded.price_currency,
                cost_amount = excluded.cost_amount,
                updated_at = excluded.updated_at
            """,
            (
                holding.chain,
                holding.token_address,
                holding.symbol,
                holding.quantity,
                holding.entry_price,
                holding.price_currency,
                holding.cost_amount,
                utc_now(),
            ),
        )
        self.connection.execute(
            "DELETE FROM portfolio_states WHERE chain = ? AND token_address = ?",
            (holding.chain, holding.token_address),
        )
        self.connection.commit()

    def is_launch_guard_owned(self, *, chain: str, token_address: str) -> bool:
        """True if this ledger's own buy flow has ever recorded a fill for
        this mint (owned_holdings is upserted at buy confirmation and never
        deleted, even after a full sell - see save_owned_holding). A wallet
        holding that fails this check was acquired some other way (a
        separate copy-trading bot, a manual purchase, ...) and this ledger
        has no cost-basis or intent behind it."""
        return self.connection.execute(
            "SELECT 1 FROM owned_holdings WHERE chain = ? AND token_address = ?",
            (chain, token_address),
        ).fetchone() is not None

    def classify_external_wallet_position(
        self, token_address: str, *, current_value_usd: float,
        known_purchase_ranges_usd: tuple[tuple[float, float], ...] = (),
    ) -> str:
        """Classify a wallet holding this ledger never bought, the first
        time it's ever seen, and remember that classification forever -
        not re-derived on later cycles, since current value drifts away
        from whatever was actually paid within minutes on these tokens,
        making a later check meaningless. known_purchase_ranges_usd is a
        set of (min, max) USD amount ranges a known external buyer (e.g. a
        copy-trading bot with a small, predictable set of position sizes)
        is known to use - a first-seen value landing in one of these is
        classified "external_known", anything else "external_personal".
        Only call this for a mint that already failed is_launch_guard_owned.
        """
        existing = self.connection.execute(
            "SELECT classification FROM external_wallet_classifications WHERE token_address = ?",
            (token_address,),
        ).fetchone()
        if existing is not None:
            return existing[0]
        classification = (
            "external_known"
            if any(lo <= current_value_usd <= hi for lo, hi in known_purchase_ranges_usd)
            else "external_personal"
        )
        self.connection.execute(
            "INSERT INTO external_wallet_classifications"
            "(token_address, first_seen_value_usd, classification, classified_at) "
            "VALUES (?, ?, ?, ?)",
            (token_address, current_value_usd, classification, utc_now()),
        )
        self.connection.commit()
        return classification

    def load_owned_holdings(self, chain: str | None = None) -> list[OwnedHolding]:
        if chain is None:
            rows = self.connection.execute(
                "SELECT * FROM owned_holdings ORDER BY chain, symbol"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM owned_holdings WHERE chain = ? ORDER BY symbol",
                (chain,),
            ).fetchall()
        return [
            OwnedHolding(
                chain=str(row["chain"]),
                token_address=str(row["token_address"]),
                symbol=str(row["symbol"]),
                quantity=float(row["quantity"]),
                entry_price=(
                    float(row["entry_price"])
                    if row["entry_price"] is not None
                    else None
                ),
                price_currency=(
                    str(row["price_currency"])
                    if row["price_currency"] is not None
                    else None
                ),
                cost_amount=(
                    float(row["cost_amount"])
                    if row["cost_amount"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    def save_portfolio_state(
        self,
        *,
        chain: str,
        token_address: str,
        peak_price: float,
        baseline_liquidity_usd: float,
        below_sell_minimum: bool = False,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO portfolio_states(
                chain, token_address, peak_price,
                baseline_liquidity_usd, below_sell_minimum, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chain, token_address) DO UPDATE SET
                peak_price = excluded.peak_price,
                baseline_liquidity_usd = excluded.baseline_liquidity_usd,
                below_sell_minimum = excluded.below_sell_minimum,
                updated_at = excluded.updated_at
            """,
            (
                chain,
                token_address,
                peak_price,
                baseline_liquidity_usd,
                int(below_sell_minimum),
                utc_now(),
            ),
        )
        self.connection.commit()

    def load_portfolio_states(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT chain, token_address, peak_price, baseline_liquidity_usd, "
            "below_sell_minimum "
            "FROM portfolio_states"
        ).fetchall()
        return [dict(row) for row in rows]

    def record_loss_sale(
        self, *, sale_id: str, token_address: str, symbol: str,
        cost_usd: float, proceeds_usd: float, quantity: float,
        sold_at_epoch: float, source: str = "manual",
        exit_liquidity_usd: float | None = None,
    ) -> dict[str, Any]:
        if not sale_id.strip() or not token_address.strip() or not symbol.strip():
            raise ValueError("sale ID, mint, and symbol are required")
        if not all(math.isfinite(v) for v in (cost_usd, proceeds_usd, quantity, sold_at_epoch)):
            raise ValueError("sale amounts and time must be finite")
        if not 0 < proceeds_usd < cost_usd or quantity <= 0 or sold_at_epoch <= 0:
            raise ValueError("a net-loss sale requires positive quantity, proceeds, and cost above proceeds")
        if exit_liquidity_usd is not None and (not math.isfinite(exit_liquidity_usd) or exit_liquidity_usd < 0):
            raise ValueError("exit liquidity must be nonnegative and finite")
        existing = self.connection.execute(
            "SELECT * FROM loss_sale_reviews WHERE sale_id = ?", (sale_id,)
        ).fetchone()
        if existing is not None:
            if (existing["token_address"] != token_address or
                    float(existing["cost_usd"]) != cost_usd or
                    float(existing["proceeds_usd"]) != proceeds_usd or
                    float(existing["quantity"]) != quantity):
                raise ValueError("sale ID is already recorded with different amounts")
            return dict(existing)
        self.connection.execute(
            """INSERT INTO loss_sale_reviews
            (sale_id, chain, token_address, symbol, source, cost_usd,
             proceeds_usd, quantity, sold_at_epoch, exit_liquidity_usd,
             lowest_price_usd, updated_at)
            VALUES (?, 'solana', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sale_id, token_address, symbol, source, cost_usd, proceeds_usd,
             quantity, sold_at_epoch, exit_liquidity_usd,
             proceeds_usd / quantity, utc_now()),
        )
        self.connection.commit()
        return dict(self.connection.execute(
            "SELECT * FROM loss_sale_reviews WHERE sale_id = ?", (sale_id,)
        ).fetchone())

    def loss_sale_reviews(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM loss_sale_reviews ORDER BY sold_at_epoch DESC"
        ).fetchall()]

    def update_loss_sale_review(
        self, sale_id: str, *, price_usd: float, qualified: bool,
        reason: str, confirmation_required: int,
    ) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM loss_sale_reviews WHERE sale_id = ?", (sale_id,)
        ).fetchone()
        if row is None or not 0 < price_usd or not math.isfinite(price_usd):
            raise ValueError("loss review requires a recorded sale and positive quote")
        count = int(row["confirmation_count"]) + 1 if qualified else 0
        decision = "REBUY REVIEW" if count >= confirmation_required else "REBUY WATCH"
        self.connection.execute(
            """UPDATE loss_sale_reviews SET lowest_price_usd = ?,
            last_price_usd = ?, confirmation_count = ?, decision = ?,
            reason = ?, updated_at = ? WHERE sale_id = ?""",
            (min(float(row["lowest_price_usd"]), price_usd), price_usd,
             count, decision, reason, utc_now(), sale_id),
        )
        self.connection.commit()
        return dict(self.connection.execute(
            "SELECT * FROM loss_sale_reviews WHERE sale_id = ?", (sale_id,)
        ).fetchone())

    def reset_loss_sale_review(self, sale_id: str, reason: str) -> None:
        self.connection.execute(
            """UPDATE loss_sale_reviews SET confirmation_count = 0,
            decision = 'REBUY WATCH', reason = ?, last_price_usd = NULL,
            updated_at = ? WHERE sale_id = ?""",
            (reason, utc_now(), sale_id),
        )
        self.connection.commit()

    def arm_auto_sell(
        self,
        token_address: str,
        *,
        chain: str = "solana",
        reset_stage: bool = False,
    ) -> None:
        holding = self.connection.execute(
            "SELECT entry_price, price_currency, cost_amount "
            "FROM owned_holdings WHERE chain = ? AND token_address = ?",
            (chain, token_address),
        ).fetchone()
        if holding is None:
            raise ValueError("import the holding and USD cost basis before arming")
        if (
            holding["price_currency"] != "USD"
            or holding["entry_price"] is None
            or float(holding["entry_price"]) <= 0
            or holding["cost_amount"] is None
            or float(holding["cost_amount"]) <= 0
        ):
            raise ValueError("auto-sell requires a positive USD cost basis")
        self.connection.execute(
            """
            INSERT INTO auto_sell_policies(
                chain, token_address, armed, stage, updated_at
            ) VALUES (?, ?, 1, 0, ?)
            ON CONFLICT(chain, token_address) DO UPDATE SET
                armed = 1,
                stage = CASE
                    WHEN ? THEN 0 ELSE auto_sell_policies.stage
                END,
                last_signature = CASE
                    WHEN ? THEN NULL ELSE auto_sell_policies.last_signature
                END,
                updated_at = excluded.updated_at
            """,
            (
                chain,
                token_address,
                utc_now(),
                int(reset_stage),
                int(reset_stage),
            ),
        )
        self.connection.commit()

    def disarm_auto_sell(
        self, token_address: str, *, chain: str = "solana"
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO auto_sell_policies(
                chain, token_address, armed, stage, updated_at
            ) VALUES (?, ?, 0, 0, ?)
            ON CONFLICT(chain, token_address) DO UPDATE SET
                armed = 0,
                updated_at = excluded.updated_at
            """,
            (chain, token_address, utc_now()),
        )
        self.connection.commit()

    def allow_auto_sell_signals(
        self, token_address: str, *, chain: str = "solana"
    ) -> None:
        """Remove a per-mint block for wallet-wide portfolio-signal exits."""
        self.connection.execute(
            """
            INSERT INTO auto_sell_policies(
                chain, token_address, armed, stage, updated_at
            ) VALUES (?, ?, 1, 0, ?)
            ON CONFLICT(chain, token_address) DO UPDATE SET
                armed = 1,
                updated_at = excluded.updated_at
            """,
            (chain, token_address, utc_now()),
        )
        self.connection.commit()

    def load_auto_sell_policy(
        self, token_address: str, *, chain: str = "solana"
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM auto_sell_policies "
            "WHERE chain = ? AND token_address = ?",
            (chain, token_address),
        ).fetchone()
        return dict(row) if row is not None else None

    def record_auto_sell_signal_confirmation(
        self,
        *,
        token_address: str,
        decision: str,
        reason: str,
        observed_at_epoch: float,
        max_gap_seconds: float,
        chain: str = "solana",
    ) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM auto_sell_signal_confirmations "
            "WHERE chain = ? AND token_address = ?",
            (chain, token_address),
        ).fetchone()
        consecutive = 1
        if (
            row is not None
            and str(row["decision"]) == decision
            and observed_at_epoch >= float(row["last_seen_epoch"])
            and observed_at_epoch - float(row["last_seen_epoch"])
            <= max_gap_seconds
        ):
            consecutive = int(row["consecutive_polls"]) + 1
        self.connection.execute(
            """
            INSERT INTO auto_sell_signal_confirmations(
                chain, token_address, decision, consecutive_polls, reason,
                last_seen_epoch, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chain, token_address) DO UPDATE SET
                decision = excluded.decision,
                consecutive_polls = excluded.consecutive_polls,
                reason = excluded.reason,
                last_seen_epoch = excluded.last_seen_epoch,
                updated_at = excluded.updated_at
            """,
            (
                chain,
                token_address,
                decision,
                consecutive,
                reason,
                observed_at_epoch,
                utc_now(),
            ),
        )
        self.connection.commit()
        confirmed = self.connection.execute(
            "SELECT * FROM auto_sell_signal_confirmations "
            "WHERE chain = ? AND token_address = ?",
            (chain, token_address),
        ).fetchone()
        assert confirmed is not None
        return dict(confirmed)

    def clear_auto_sell_signal_confirmation(
        self, token_address: str, *, chain: str = "solana"
    ) -> None:
        self.connection.execute(
            "DELETE FROM auto_sell_signal_confirmations "
            "WHERE chain = ? AND token_address = ?",
            (chain, token_address),
        )
        self.connection.commit()

    def auto_sell_cycle(
        self, token_address: str, *, chain: str = "solana"
    ) -> int:
        row = self.connection.execute(
            "SELECT completed_rebuys FROM auto_rebuy_watches "
            "WHERE chain = ? AND token_address = ?",
            (chain, token_address),
        ).fetchone()
        return int(row["completed_rebuys"]) if row is not None else 0

    def auto_sell_status(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT p.chain, p.token_address, h.symbol, p.armed, p.stage,
                   p.last_signature, p.updated_at
            FROM auto_sell_policies p
            LEFT JOIN owned_holdings h
              ON h.chain = p.chain AND h.token_address = p.token_address
            ORDER BY p.chain, h.symbol, p.token_address
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def auto_sell_review_status(self) -> dict[str, list[dict[str, Any]]]:
        batches = self.connection.execute(
            """
            SELECT batch_key, chain, token_address, symbol, stage, status,
                   target_raw, full_exit, sold_raw, proceeds_usdc_raw,
                   next_chunk_index,
                   last_signature, error, created_at, updated_at
            FROM auto_sell_batches
            WHERE status IN ('REVIEW', 'PAUSED')
            ORDER BY updated_at DESC
            """
        ).fetchall()
        executions = self.connection.execute(
            """
            SELECT event_key, chain, token_address, symbol, stage, status,
                   requested_raw, balance_before_raw, expected_output_raw,
                   signature, error, created_at, updated_at
            FROM auto_sell_executions
            WHERE status IN ('REVIEW', 'CLEARED_NO_TRANSACTION')
            ORDER BY updated_at DESC
            """
        ).fetchall()
        return {
            "batches": [dict(row) for row in batches],
            "executions": [dict(row) for row in executions],
        }

    def load_auto_sell_review(self, batch_key: str) -> dict[str, Any]:
        batch = self.connection.execute(
            "SELECT * FROM auto_sell_batches WHERE batch_key = ?",
            (batch_key,),
        ).fetchone()
        if batch is None:
            raise ValueError("auto-sell batch was not found")
        chunk_index = int(batch["next_chunk_index"])
        event_key = f"{batch_key}:chunk:{chunk_index}"
        execution = self.connection.execute(
            "SELECT * FROM auto_sell_executions WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        if execution is None:
            execution = self.connection.execute(
                "SELECT * FROM auto_sell_executions "
                "WHERE event_key LIKE ? ORDER BY updated_at DESC LIMIT 1",
                (f"{batch_key}:chunk:%",),
            ).fetchone()
        return {
            "batch": dict(batch),
            "execution": dict(execution) if execution is not None else None,
        }

    def resolve_auto_sell_review(
        self,
        *,
        batch_key: str,
        confirmed_no_transaction: bool,
        verified_balance_raw: int,
    ) -> dict[str, Any]:
        if not confirmed_no_transaction:
            raise ValueError("explicit no-transaction confirmation is required")
        review = self.load_auto_sell_review(batch_key)
        batch = review["batch"]
        execution = review["execution"]
        if batch["status"] != "REVIEW":
            raise ValueError("auto-sell batch is not awaiting review")
        if execution is None or execution["status"] != "REVIEW":
            raise ValueError("auto-sell execution is not awaiting review")
        if execution.get("signature"):
            raise ValueError(
                "review has a transaction signature; inspect it on-chain"
            )
        balance_before = execution.get("balance_before_raw")
        if balance_before is None and bool(batch["full_exit"]):
            balance_before = int(batch["target_raw"]) - int(batch["sold_raw"])
        if balance_before is None:
            raise ValueError(
                "the pre-execution balance is unavailable; review cannot be "
                "resolved automatically"
            )
        if verified_balance_raw != int(balance_before):
            raise ValueError(
                "current on-chain balance differs from the pre-execution balance"
            )

        now = utc_now()
        prior_error = str(execution.get("error") or "execution outcome reviewed")
        review_note = (
            f"{prior_error}; operator confirmed no transaction after "
            f"on-chain balance reconciliation"
        )[:500]
        with self.connection:
            self.connection.execute(
                "UPDATE auto_sell_executions "
                "SET status = 'CLEARED_NO_TRANSACTION', error = ?, "
                "updated_at = ? WHERE event_key = ? AND status = 'REVIEW'",
                (review_note, now, execution["event_key"]),
            )
            self.connection.execute(
                "UPDATE auto_sell_batches "
                "SET status = 'PAUSED', next_chunk_index = ?, error = ?, "
                "updated_at = ? WHERE batch_key = ? AND status = 'REVIEW'",
                (
                    int(batch["next_chunk_index"]) + 1,
                    "review resolved; batch remains paused",
                    now,
                    batch_key,
                ),
            )
        return self.load_auto_sell_review(batch_key)["batch"]

    def resume_auto_sell_batch(
        self, *, batch_key: str, confirmed_monitor_stopped: bool
    ) -> dict[str, Any]:
        if not confirmed_monitor_stopped:
            raise ValueError("explicit stopped-monitor confirmation is required")
        batch = self.connection.execute(
            "SELECT * FROM auto_sell_batches WHERE batch_key = ?",
            (batch_key,),
        ).fetchone()
        if batch is None:
            raise ValueError("auto-sell batch was not found")
        if batch["status"] != "PAUSED":
            raise ValueError("only a resolved paused batch can be resumed")
        self.connection.execute(
            "UPDATE auto_sell_batches SET status = 'ACTIVE', error = NULL, "
            "updated_at = ? WHERE batch_key = ? AND status = 'PAUSED'",
            (utc_now(), batch_key),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT * FROM auto_sell_batches WHERE batch_key = ?",
            (batch_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("could not reload the auto-sell batch")
        return dict(row)

    def begin_auto_sell_execution(
        self,
        *,
        event_key: str,
        chain: str,
        token_address: str,
        symbol: str,
        stage: int,
        requested_raw: int,
        expected_output_raw: int,
        balance_before_raw: int | None = None,
    ) -> bool:
        now = utc_now()
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO auto_sell_executions(
                event_key, chain, token_address, symbol, stage, status,
                requested_raw, balance_before_raw, expected_output_raw,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?)
            """,
            (
                event_key,
                chain,
                token_address,
                symbol,
                stage,
                requested_raw,
                balance_before_raw,
                expected_output_raw,
                now,
                now,
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def load_or_create_auto_sell_batch(
        self,
        *,
        batch_key: str,
        chain: str,
        token_address: str,
        symbol: str,
        stage: int,
        target_raw: int,
        full_exit: bool,
    ) -> dict[str, Any]:
        if target_raw <= 0:
            raise ValueError("auto-sell batch target must be positive")
        now = utc_now()
        self.connection.execute(
            """
            INSERT OR IGNORE INTO auto_sell_batches(
                batch_key, chain, token_address, symbol, stage, status,
                target_raw, full_exit, sold_raw, next_chunk_index,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?, 0, 0, ?, ?)
            """,
            (
                batch_key,
                chain,
                token_address,
                symbol,
                stage,
                target_raw,
                int(full_exit),
                now,
                now,
            ),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT * FROM auto_sell_batches WHERE batch_key = ?",
            (batch_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("could not load the auto-sell batch")
        return dict(row)

    def complete_auto_sell_chunk(
        self,
        *,
        batch_key: str,
        event_key: str,
        signature: str,
        sold_raw: int,
        output_usdc_raw: int = 0,
    ) -> bool:
        if sold_raw <= 0:
            raise ValueError("confirmed auto-sell chunk must be positive")
        if output_usdc_raw < 0:
            raise ValueError("confirmed auto-sell output cannot be negative")
        execution = self.connection.execute(
            "SELECT status FROM auto_sell_executions WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        batch = self.connection.execute(
            "SELECT * FROM auto_sell_batches WHERE batch_key = ?",
            (batch_key,),
        ).fetchone()
        if execution is None or execution["status"] != "PENDING":
            raise ValueError("auto-sell chunk execution is not pending")
        if batch is None or batch["status"] != "ACTIVE":
            raise ValueError("auto-sell batch is not active")
        total_sold = min(
            int(batch["target_raw"]), int(batch["sold_raw"]) + sold_raw
        )
        completed = total_sold >= int(batch["target_raw"])
        now = utc_now()
        with self.connection:
            self.connection.execute(
                "UPDATE auto_sell_executions SET status = 'CONFIRMED', "
                "signature = ?, updated_at = ? WHERE event_key = ?",
                (signature, now, event_key),
            )
            self.connection.execute(
                """
                UPDATE auto_sell_batches
                SET status = ?, sold_raw = ?,
                    proceeds_usdc_raw = proceeds_usdc_raw + ?,
                    next_chunk_index = ?, last_signature = ?, updated_at = ?
                WHERE batch_key = ? AND status = 'ACTIVE'
                """,
                (
                    "CONFIRMED" if completed else "ACTIVE",
                    total_sold,
                    output_usdc_raw,
                    int(batch["next_chunk_index"]) + 1,
                    signature,
                    now,
                    batch_key,
                ),
            )
        return completed

    def load_auto_sell_batch(self, batch_key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM auto_sell_batches WHERE batch_key = ?",
            (batch_key,),
        ).fetchone()
        return dict(row) if row is not None else None

    def freeze_auto_sell_chunk(
        self,
        *,
        batch_key: str,
        event_key: str,
        error: str,
        signature: str | None = None,
    ) -> None:
        now = utc_now()
        with self.connection:
            self.connection.execute(
                "UPDATE auto_sell_executions SET status = 'REVIEW', error = ?, "
                "signature = COALESCE(?, signature), updated_at = ? "
                "WHERE event_key = ? AND status = 'PENDING'",
                (error[:500], signature, now, event_key),
            )
            self.connection.execute(
                "UPDATE auto_sell_batches SET status = 'REVIEW', error = ?, "
                "updated_at = ? WHERE batch_key = ? AND status = 'ACTIVE'",
                (error[:500], now, batch_key),
            )

    def complete_auto_sell_execution(
        self, *, event_key: str, signature: str, next_stage: int
    ) -> None:
        row = self.connection.execute(
            "SELECT chain, token_address, stage FROM auto_sell_executions "
            "WHERE event_key = ? AND status = 'PENDING'",
            (event_key,),
        ).fetchone()
        if row is None:
            raise ValueError("auto-sell execution is not pending")
        now = utc_now()
        with self.connection:
            self.connection.execute(
                "UPDATE auto_sell_executions SET status = 'CONFIRMED', "
                "signature = ?, updated_at = ? WHERE event_key = ?",
                (signature, now, event_key),
            )
            self.connection.execute(
                """
                UPDATE auto_sell_policies
                SET stage = ?, last_signature = ?, updated_at = ?
                WHERE chain = ? AND token_address = ? AND stage = ?
                """,
                (
                    next_stage,
                    signature,
                    now,
                    row["chain"],
                    row["token_address"],
                    row["stage"],
                ),
            )

    def freeze_auto_sell_execution(
        self,
        *,
        event_key: str,
        error: str,
        signature: str | None = None,
    ) -> None:
        self.connection.execute(
            "UPDATE auto_sell_executions SET status = 'REVIEW', error = ?, "
            "signature = COALESCE(?, signature), updated_at = ? "
            "WHERE event_key = ? AND status = 'PENDING'",
            (error[:500], signature, utc_now(), event_key),
        )
        self.connection.commit()

    def arm_auto_buy(
        self,
        token_address: str,
        symbol: str,
        *,
        chain: str = "solana",
    ) -> None:
        token_address = token_address.strip()
        symbol = symbol.strip().upper()
        if not token_address or not symbol:
            raise ValueError("auto-buy requires a mint and symbol")
        open_position = self.connection.execute(
            "SELECT 1 FROM auto_buy_positions WHERE chain = ? "
            "AND token_address = ? AND status = 'OPEN'",
            (chain, token_address),
        ).fetchone()
        if open_position is not None:
            raise ValueError("this mint already has an open bot position")
        self.connection.execute(
            """
            INSERT INTO auto_buy_policies(
                chain, token_address, symbol, armed, updated_at
            ) VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(chain, token_address) DO UPDATE SET
                symbol = excluded.symbol,
                armed = 1,
                updated_at = excluded.updated_at
            """,
            (chain, token_address, symbol, utc_now()),
        )
        self.connection.commit()

    def start_auto_buy_discovery_watch(
        self,
        *,
        token_address: str,
        symbol: str,
        launch_payload: Mapping[str, Any],
        first_seen_epoch: float | None = None,
        first_check_epoch: float | None = None,
        expires_at_epoch: float,
        max_active: int,
        chain: str = "solana",
    ) -> str:
        """Persist a launch watch, evicting the weakest watch at capacity."""
        token_address = token_address.strip()
        symbol = symbol.strip() or token_address[:8]
        if not token_address:
            raise ValueError("auto-buy discovery watch requires a token address")
        if max_active < 1:
            raise ValueError("auto-buy discovery watch capacity must be positive")
        observed = time.time() if first_seen_epoch is None else first_seen_epoch
        first_check = observed if first_check_epoch is None else first_check_epoch
        if expires_at_epoch <= observed:
            raise ValueError("auto-buy discovery watch expiry must be in the future")
        launch_json = json.dumps(
            dict(launch_payload), sort_keys=True, separators=(",", ":")
        )
        existing = self.connection.execute(
            "SELECT status FROM auto_buy_discovery_watches "
            "WHERE chain = ? AND token_address = ?",
            (chain, token_address),
        ).fetchone()
        if existing is not None:
            return "EXISTS"

        now_text = utc_now()
        with self.connection:
            self.connection.execute(
                "UPDATE auto_buy_discovery_watches SET status = 'EXPIRED', "
                "last_reason = 'configured discovery watch lifetime elapsed', "
                "updated_at = ? WHERE status IN "
                "('WATCHING', 'TRACKING', 'QUALIFIED') "
                "AND expires_at_epoch <= ?",
                (now_text, observed),
            )
            active_count = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM auto_buy_discovery_watches "
                    "WHERE status IN ('WATCHING', 'TRACKING', 'QUALIFIED')"
                ).fetchone()[0]
            )
            if active_count >= max_active:
                victim = self.connection.execute(
                    "SELECT chain, token_address FROM auto_buy_discovery_watches "
                    "WHERE status IN ('WATCHING', 'TRACKING', 'QUALIFIED') "
                    "ORDER BY CASE status WHEN 'WATCHING' THEN 0 "
                    "WHEN 'TRACKING' THEN 1 ELSE 2 END, "
                    "intelligence_score ASC, signal_score ASC, "
                    "first_seen_epoch ASC LIMIT 1"
                ).fetchone()
                if victim is not None:
                    self.connection.execute(
                        "UPDATE auto_buy_discovery_watches "
                        "SET status = 'EVICTED', "
                        "last_reason = 'active discovery watch capacity reached', "
                        "updated_at = ? WHERE chain = ? AND token_address = ?",
                        (now_text, victim["chain"], victim["token_address"]),
                    )
            self.connection.execute(
                """
                INSERT INTO auto_buy_discovery_watches(
                    chain, token_address, symbol, status, launch_json,
                    first_seen_epoch, next_check_epoch, expires_at_epoch,
                    last_reason, created_at, updated_at
                ) VALUES (?, ?, ?, 'WATCHING', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chain,
                    token_address,
                    symbol,
                    launch_json,
                    observed,
                    first_check,
                    expires_at_epoch,
                    "waiting for first market observation",
                    now_text,
                    now_text,
                ),
            )
        return "CREATED"

    def expire_auto_buy_discovery_watches(
        self, *, now_epoch: float | None = None
    ) -> int:
        observed = time.time() if now_epoch is None else now_epoch
        cursor = self.connection.execute(
            "UPDATE auto_buy_discovery_watches SET status = 'EXPIRED', "
            "last_reason = 'configured discovery watch lifetime elapsed', "
            "updated_at = ? WHERE status IN "
            "('WATCHING', 'TRACKING', 'QUALIFIED') "
            "AND expires_at_epoch <= ?",
            (utc_now(), observed),
        )
        self.connection.commit()
        return cursor.rowcount

    def due_auto_buy_discovery_watches(
        self, *, now_epoch: float | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        observed = time.time() if now_epoch is None else now_epoch
        self.expire_auto_buy_discovery_watches(now_epoch=observed)
        rows = self.connection.execute(
            "SELECT * FROM auto_buy_discovery_watches "
            "WHERE status IN ('WATCHING', 'TRACKING', 'QUALIFIED') "
            "AND next_check_epoch <= ? AND expires_at_epoch > ? "
            "ORDER BY next_check_epoch, first_seen_epoch LIMIT ?",
            (observed, observed, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def active_auto_buy_discovery_watches(
        self, *, now_epoch: float | None = None
    ) -> list[dict[str, Any]]:
        observed = time.time() if now_epoch is None else now_epoch
        self.expire_auto_buy_discovery_watches(now_epoch=observed)
        rows = self.connection.execute(
            "SELECT * FROM auto_buy_discovery_watches "
            "WHERE status IN ('WATCHING', 'TRACKING', 'QUALIFIED') "
            "ORDER BY signal_score DESC, intelligence_score DESC, "
            "first_seen_epoch DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def load_auto_buy_discovery_watch(
        self, token_address: str, *, chain: str = "solana"
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM auto_buy_discovery_watches "
            "WHERE chain = ? AND token_address = ?",
            (chain, token_address),
        ).fetchone()
        return dict(row) if row is not None else None

    def record_auto_buy_discovery_observation(
        self,
        *,
        token_address: str,
        symbol: str,
        status: str,
        next_check_epoch: float,
        reason: str,
        quote_available: bool,
        candidate_json: str | None = None,
        tier: str | None = None,
        intelligence_score: int = 0,
        signal_score: int = 0,
        decision: str | None = None,
        liquidity_usd: float | None = None,
        observed_epoch: float | None = None,
        chain: str = "solana",
    ) -> None:
        if status not in {"WATCHING", "TRACKING", "QUALIFIED"}:
            raise ValueError("invalid active auto-buy discovery status")
        observed = time.time() if observed_epoch is None else observed_epoch
        self.connection.execute(
            """
            UPDATE auto_buy_discovery_watches
            SET symbol = ?, status = ?,
                candidate_json = COALESCE(?, candidate_json),
                tier = ?, intelligence_score = ?, signal_score = ?,
                decision = ?, liquidity_usd = ?, attempts = attempts + 1,
                quote_failures = quote_failures + ?,
                last_seen_epoch = CASE WHEN ? THEN ? ELSE last_seen_epoch END,
                next_check_epoch = ?, last_reason = ?, updated_at = ?
            WHERE chain = ? AND token_address = ?
              AND status IN ('WATCHING', 'TRACKING', 'QUALIFIED')
            """,
            (
                symbol,
                status,
                candidate_json,
                tier,
                intelligence_score,
                signal_score,
                decision,
                liquidity_usd,
                int(not quote_available),
                int(quote_available),
                observed,
                next_check_epoch,
                reason[:500],
                utc_now(),
                chain,
                token_address,
            ),
        )
        self.connection.commit()

    def save_auto_buy_discovery_candidate(
        self,
        *,
        token_address: str,
        symbol: str,
        candidate_json: str,
        tier: str,
        intelligence_score: int,
        signal_score: int,
        decision: str,
        liquidity_usd: float | None,
        next_check_epoch: float,
        reason: str,
        status: str = "TRACKING",
        observed_epoch: float | None = None,
        chain: str = "solana",
    ) -> None:
        if status not in {"TRACKING", "QUALIFIED"}:
            raise ValueError("invalid tracked auto-buy discovery status")
        observed = time.time() if observed_epoch is None else observed_epoch
        self.connection.execute(
            """
            UPDATE auto_buy_discovery_watches
            SET symbol = ?, status = ?, candidate_json = ?, tier = ?,
                intelligence_score = ?, signal_score = ?, decision = ?,
                liquidity_usd = ?, last_seen_epoch = ?, next_check_epoch = ?,
                last_reason = ?, updated_at = ?
            WHERE chain = ? AND token_address = ?
              AND status IN ('WATCHING', 'TRACKING', 'QUALIFIED')
            """,
            (
                symbol,
                status,
                candidate_json,
                tier,
                intelligence_score,
                signal_score,
                decision,
                liquidity_usd,
                observed,
                next_check_epoch,
                reason[:500],
                utc_now(),
                chain,
                token_address,
            ),
        )
        self.connection.commit()

    def mark_auto_buy_discovery_watch(
        self,
        token_address: str,
        *,
        status: str,
        reason: str,
        chain: str = "solana",
    ) -> bool:
        allowed = {
            "WATCHING",
            "TRACKING",
            "QUALIFIED",
            "BOUGHT",
            "REVIEW",
            "CANCELLED",
            "EXPIRED",
            "EVICTED",
        }
        if status not in allowed:
            raise ValueError("invalid auto-buy discovery watch status")
        cursor = self.connection.execute(
            "UPDATE auto_buy_discovery_watches SET status = ?, "
            "last_reason = ?, updated_at = ? "
            "WHERE chain = ? AND token_address = ? AND status IN "
            "('WATCHING', 'TRACKING', 'QUALIFIED')",
            (status, reason[:500], utc_now(), chain, token_address),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def cancel_auto_buy_discovery_watch(
        self, token_address: str, *, chain: str = "solana"
    ) -> bool:
        cursor = self.connection.execute(
            "UPDATE auto_buy_discovery_watches SET status = 'CANCELLED', "
            "last_reason = 'cancelled by operator', updated_at = ? "
            "WHERE chain = ? AND token_address = ? AND status IN "
            "('WATCHING', 'TRACKING', 'QUALIFIED')",
            (utc_now(), chain, token_address),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def auto_buy_discovery_status(self) -> dict[str, list[dict[str, Any]]]:
        self.expire_auto_buy_discovery_watches()
        columns = (
            "chain, token_address, symbol, status, tier, "
            "intelligence_score, signal_score, decision, liquidity_usd, "
            "attempts, quote_failures, first_seen_epoch, last_seen_epoch, "
            "next_check_epoch, expires_at_epoch, last_reason, updated_at"
        )
        active = self.connection.execute(
            f"SELECT {columns} FROM auto_buy_discovery_watches "
            "WHERE status IN ('WATCHING', 'TRACKING', 'QUALIFIED') "
            "ORDER BY signal_score DESC, intelligence_score DESC, "
            "first_seen_epoch DESC"
        ).fetchall()
        terminal = self.connection.execute(
            f"SELECT {columns} FROM auto_buy_discovery_watches "
            "WHERE status NOT IN ('WATCHING', 'TRACKING', 'QUALIFIED') "
            "ORDER BY updated_at DESC LIMIT 50"
        ).fetchall()
        return {
            "active": [dict(row) for row in active],
            "recent_terminal": [dict(row) for row in terminal],
        }

    def disarm_auto_buy(
        self, token_address: str, *, chain: str = "solana"
    ) -> None:
        self.connection.execute(
            "UPDATE auto_buy_policies SET armed = 0, updated_at = ? "
            "WHERE chain = ? AND token_address = ?",
            (utc_now(), chain, token_address),
        )
        self.connection.commit()

    def load_auto_buy_policy(
        self, token_address: str, *, chain: str = "solana"
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM auto_buy_policies "
            "WHERE chain = ? AND token_address = ?",
            (chain, token_address),
        ).fetchone()
        return dict(row) if row is not None else None

    def preview_auto_buy_budget(
        self,
        *,
        seed_size_usdc_raw: int,
        max_seed_buys: int,
        max_open_positions: int,
        minimum_reinvest_usdc_raw: int = 1_000_000,
    ) -> tuple[int, str]:
        pending = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM auto_buy_executions "
                "WHERE status = 'PENDING'"
            ).fetchone()[0]
        )
        opened = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM auto_buy_positions WHERE status = 'OPEN'"
            ).fetchone()[0]
        )
        if opened + pending >= max_open_positions:
            raise ValueError("maximum open bot positions has been reached")
        fund = self.connection.execute(
            "SELECT seed_buys_used, reinvest_available_usdc_raw "
            "FROM auto_buy_fund WHERE id = 1"
        ).fetchone()
        assert fund is not None
        if int(fund["seed_buys_used"]) < max_seed_buys:
            return seed_size_usdc_raw, "seed"
        available = int(fund["reinvest_available_usdc_raw"])
        amount = min(seed_size_usdc_raw, available)
        if amount < minimum_reinvest_usdc_raw:
            raise ValueError(
                "reinvestment pool is below the 1.00 USDC minimum"
            )
        return amount, "reinvested_profit"

    def begin_auto_buy_execution(
        self,
        *,
        event_key: str,
        token_address: str,
        symbol: str,
        funding_source: str,
        input_usdc_raw: int,
        expected_output_raw: int,
        chain: str = "solana",
    ) -> bool:
        now = utc_now()
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO auto_buy_executions(
                event_key, chain, token_address, symbol, funding_source,
                status, input_usdc_raw, expected_output_raw,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)
            """,
            (
                event_key,
                chain,
                token_address,
                symbol,
                funding_source,
                input_usdc_raw,
                expected_output_raw,
                now,
                now,
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def complete_auto_buy_execution(
        self,
        *,
        event_key: str,
        signature: str,
        actual_output_raw: int,
        output_decimals: int,
    ) -> None:
        row = self.connection.execute(
            "SELECT * FROM auto_buy_executions "
            "WHERE event_key = ? AND status = 'PENDING'",
            (event_key,),
        ).fetchone()
        if row is None:
            raise ValueError("auto-buy execution is not pending")
        if actual_output_raw <= 0 or output_decimals < 0:
            raise ValueError("confirmed auto-buy returned an invalid token amount")
        now = utc_now()
        with self.connection:
            self.connection.execute(
                "UPDATE auto_buy_executions SET status = 'CONFIRMED', "
                "actual_output_raw = ?, output_decimals = ?, signature = ?, "
                "updated_at = ? WHERE event_key = ?",
                (
                    actual_output_raw,
                    output_decimals,
                    signature,
                    now,
                    event_key,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO auto_buy_positions(
                    chain, token_address, symbol, status, cost_usdc_raw,
                    token_amount_raw, remaining_raw, token_decimals,
                    buy_signature, opened_at, updated_at
                ) VALUES (?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["chain"],
                    row["token_address"],
                    row["symbol"],
                    row["input_usdc_raw"],
                    actual_output_raw,
                    actual_output_raw,
                    output_decimals,
                    signature,
                    now,
                    now,
                ),
            )
            if row["funding_source"] == "seed":
                self.connection.execute(
                    "UPDATE auto_buy_fund SET seed_buys_used = "
                    "seed_buys_used + 1, updated_at = ? WHERE id = 1",
                    (now,),
                )
            else:
                self.connection.execute(
                    "UPDATE auto_buy_fund SET reinvest_available_usdc_raw = "
                    "reinvest_available_usdc_raw - ?, updated_at = ? "
                    "WHERE id = 1 AND reinvest_available_usdc_raw >= ?",
                    (row["input_usdc_raw"], now, row["input_usdc_raw"]),
                )
            self.connection.execute(
                "UPDATE auto_buy_policies SET armed = 0, updated_at = ? "
                "WHERE chain = ? AND token_address = ?",
                (now, row["chain"], row["token_address"]),
            )

    def freeze_auto_buy_execution(
        self,
        *,
        event_key: str,
        error: str,
        signature: str | None = None,
    ) -> None:
        self.connection.execute(
            "UPDATE auto_buy_executions SET status = 'REVIEW', error = ?, "
            "signature = COALESCE(?, signature), updated_at = ? "
            "WHERE event_key = ? AND status = 'PENDING'",
            (error[:500], signature, utc_now(), event_key),
        )
        self.connection.commit()

    def record_auto_buy_sale(
        self,
        *,
        token_address: str,
        sold_raw: int,
        proceeds_usdc_raw: int,
        reinvest_pct: float,
        managed_complete: bool = False,
        chain: str = "solana",
    ) -> dict[str, int] | None:
        row = self.connection.execute(
            "SELECT * FROM auto_buy_positions WHERE chain = ? "
            "AND token_address = ? AND status = 'OPEN' "
            "ORDER BY id LIMIT 1",
            (chain, token_address),
        ).fetchone()
        if row is None:
            return None
        applied_sold = min(max(0, sold_raw), int(row["remaining_raw"]))
        if applied_sold <= 0:
            return None
        allocated_cost = round(
            int(row["cost_usdc_raw"])
            * applied_sold
            / int(row["token_amount_raw"])
        )
        profit = proceeds_usdc_raw - allocated_cost
        credit = max(0, round(profit * reinvest_pct / 100.0))
        remaining = int(row["remaining_raw"]) - applied_sold
        status = (
            "COMPLETE"
            if managed_complete and remaining > 0
            else ("CLOSED" if remaining == 0 else "OPEN")
        )
        now = utc_now()
        with self.connection:
            self.connection.execute(
                """
                UPDATE auto_buy_positions
                SET remaining_raw = ?, status = ?,
                    realized_proceeds_usdc_raw = realized_proceeds_usdc_raw + ?,
                    realized_profit_usdc_raw = realized_profit_usdc_raw + ?,
                    reinvest_credit_usdc_raw = reinvest_credit_usdc_raw + ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    remaining,
                    status,
                    proceeds_usdc_raw,
                    profit,
                    credit,
                    now,
                    row["id"],
                ),
            )
            self.connection.execute(
                """
                UPDATE auto_buy_fund
                SET reinvest_available_usdc_raw =
                        reinvest_available_usdc_raw + ?,
                    realized_profit_usdc_raw =
                        realized_profit_usdc_raw + ?,
                    updated_at = ?
                WHERE id = 1
                """,
                (credit, profit, now),
            )
        return {
            "allocated_cost_usdc_raw": allocated_cost,
            "profit_usdc_raw": profit,
            "reinvest_credit_usdc_raw": credit,
            "remaining_raw": remaining,
        }

    def reconcile_stale_auto_buy_positions(
        self, *, chain: str, held_mints: frozenset[str]
    ) -> list[dict[str, Any]]:
        """Close out any OPEN auto_buy_positions row for a mint the wallet
        no longer holds at all, once it has looked that way for
        AUTO_BUY_RECONCILE_MISSING_STREAK consecutive calls in a row.

        record_auto_buy_sale only updates this bookkeeping when a sale
        executes through a path this app itself tracks (auto-buy's own
        seller, or the live trial's execute_live_exit bridging into it) -
        a sale executed anywhere else (a manual sell in a mobile trading
        app, an external transfer) leaves the row believing the full
        original amount is still held indefinitely. Since it never sees
        that sale, its remaining_raw/status never update, and its open
        count - checked by preview_auto_buy_budget's max_open_positions
        gate - keeps counting phantom inventory forever. Confirmed live
        2026-09-24: 36 of 38 "OPEN" rows were already fully sold on-chain,
        some going back two days, permanently pinning auto-buy at capacity.

        The grace period exists because `held_mints` is only as reliable
        as the caller's own wallet-balance read: SolanaRpc.token_holdings()
        gathers two parallel per-token-program RPC calls and only raises
        if BOTH fail, so a single transient failure of just one of them
        silently returns a PARTIAL holdings list rather than an error -
        every mint held under the failed program is indistinguishable
        from "genuinely not held" for that one call. Since this method's
        write is irreversible (nothing in the codebase ever reads
        RECONCILED_EXTERNAL back to reopen a position), acting on a
        single snapshot risked permanently wiping bookkeeping for a
        position that was never actually sold. Requiring several
        consecutive misses rides out one bad read while still catching a
        genuine external sale within a few portfolio-monitor cycles.

        The real sale happened entirely outside anything this app
        tracked, so its actual proceeds/profit are unknown - this marks
        the row reconciled without fabricating a P&L, the same way the
        live trial's own ledger reconciles a position it finds sold out
        from under it ("treating as a sell outside the trial") rather
        than inventing a number.
        """
        rows = self.connection.execute(
            "SELECT id, token_address, symbol FROM auto_buy_positions "
            "WHERE chain = ? AND status = 'OPEN'",
            (chain,),
        ).fetchall()
        missing = [row for row in rows if row["token_address"] not in held_mints]
        missing_mints = {row["token_address"] for row in missing}
        for mint in list(self._auto_buy_missing_streaks):
            if mint not in missing_mints:
                self._auto_buy_missing_streaks.pop(mint, None)
        stale = []
        for row in missing:
            mint = row["token_address"]
            streak = self._auto_buy_missing_streaks.get(mint, 0) + 1
            self._auto_buy_missing_streaks[mint] = streak
            if streak >= AUTO_BUY_RECONCILE_MISSING_STREAK:
                stale.append(row)
        if not stale:
            return []
        now = utc_now()
        with self.connection:
            self.connection.executemany(
                "UPDATE auto_buy_positions SET status = 'RECONCILED_EXTERNAL', "
                "remaining_raw = 0, updated_at = ? WHERE id = ?",
                [(now, row["id"]) for row in stale],
            )
        for row in stale:
            self._auto_buy_missing_streaks.pop(row["token_address"], None)
        return [dict(row) for row in stale]

    def reconcile_stale_auto_buy_executions(
        self, *, older_than_seconds: float = 1800,
    ) -> list[dict[str, Any]]:
        """Move a PENDING auto_buy_executions row into REVIEW once it has
        sat unconfirmed for longer than a normal buy ever takes.

        preview_auto_buy_budget's max_open_positions gate counts PENDING
        executions the same as OPEN positions - a row that never reaches
        complete_auto_buy_execution (nor freeze_auto_buy_execution's own
        REVIEW path) stays PENDING forever and keeps consuming capacity
        indefinitely. Confirmed live 2026-09-24: two rows from 2026-09-22
        (CATEWALK, BITCOINU) - both mints' buys plainly did succeed under
        a *different* event_key from a later retry, since positions for
        both were later opened and sold, but the original attempt's own
        row was simply never revisited by anything to record its outcome.

        30 minutes is comfortably past how long any real quote-to-
        confirmation cycle takes, so this only ever catches an execution
        nothing is going to touch again - never one still genuinely in
        flight. Reuses freeze_auto_buy_execution's existing REVIEW state
        rather than inventing a new one, and records why in `error` since
        no outcome was ever observed for it.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        rows = self.connection.execute(
            "SELECT event_key, token_address, symbol FROM auto_buy_executions "
            "WHERE status = 'PENDING' AND created_at < ?",
            (cutoff,),
        ).fetchall()
        for row in rows:
            self.freeze_auto_buy_execution(
                event_key=row["event_key"],
                error=f"reconciled: PENDING for over {older_than_seconds:.0f}s with no "
                       "completion or freeze recorded; treated as abandoned rather than "
                       "left blocking auto-buy capacity indefinitely",
            )
        return [dict(row) for row in rows]

    def start_auto_rebuy_watch(
        self,
        *,
        token_address: str,
        symbol: str,
        sell_signature: str,
        exit_price_usd: float,
        exit_liquidity_usd: float | None,
        sale_proceeds_usdc_raw: int,
        sold_at_epoch: float,
        max_rebuys: int,
        chain: str = "solana",
    ) -> dict[str, Any] | None:
        if exit_price_usd <= 0 or sale_proceeds_usdc_raw <= 0:
            raise ValueError("auto-rebuy watch requires a confirmed sale price")
        existing = self.connection.execute(
            "SELECT * FROM auto_rebuy_watches "
            "WHERE chain = ? AND token_address = ?",
            (chain, token_address),
        ).fetchone()
        if (
            existing is not None
            and str(existing["sell_signature"]) == sell_signature
        ):
            return dict(existing)
        completed = (
            int(existing["completed_rebuys"])
            if existing is not None
            else 0
        )
        if completed >= max_rebuys:
            return None
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO auto_rebuy_watches(
                chain, token_address, symbol, status, completed_rebuys,
                cycle, sell_signature, exit_price_usd,
                exit_liquidity_usd, lowest_price_usd, last_price_usd,
                confirmation_count, sale_proceeds_usdc_raw, sold_at_epoch,
                last_seen_at_epoch, buy_signature, last_reason,
                created_at, updated_at
            ) VALUES (
                ?, ?, ?, 'WATCHING', ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?,
                NULL, NULL, 'waiting for a confirmed recovery', ?, ?
            )
            ON CONFLICT(chain, token_address) DO UPDATE SET
                symbol = excluded.symbol,
                status = 'WATCHING',
                cycle = excluded.cycle,
                sell_signature = excluded.sell_signature,
                exit_price_usd = excluded.exit_price_usd,
                exit_liquidity_usd = excluded.exit_liquidity_usd,
                lowest_price_usd = excluded.lowest_price_usd,
                last_price_usd = NULL,
                confirmation_count = 0,
                sale_proceeds_usdc_raw = excluded.sale_proceeds_usdc_raw,
                sold_at_epoch = excluded.sold_at_epoch,
                last_seen_at_epoch = NULL,
                buy_signature = NULL,
                last_reason = excluded.last_reason,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at
            """,
            (
                chain,
                token_address,
                symbol,
                completed,
                completed + 1,
                sell_signature,
                exit_price_usd,
                exit_liquidity_usd,
                exit_price_usd,
                sale_proceeds_usdc_raw,
                sold_at_epoch,
                now,
                now,
            ),
        )
        self.connection.commit()
        return self.load_auto_rebuy_watch(token_address, chain=chain)

    def load_auto_rebuy_watch(
        self, token_address: str, *, chain: str = "solana"
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM auto_rebuy_watches "
            "WHERE chain = ? AND token_address = ?",
            (chain, token_address),
        ).fetchone()
        return dict(row) if row is not None else None

    def active_auto_rebuy_watches(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM auto_rebuy_watches "
            "WHERE status IN ('WATCHING', 'READY') "
            "ORDER BY sold_at_epoch, token_address"
        ).fetchall()
        return [dict(row) for row in rows]

    def record_auto_rebuy_observation(
        self,
        *,
        token_address: str,
        current_price_usd: float,
        observed_at_epoch: float,
        qualifying: bool,
        confirmation_required: int,
        reason: str,
        chain: str = "solana",
    ) -> dict[str, Any]:
        watch = self.load_auto_rebuy_watch(token_address, chain=chain)
        if watch is None or watch["status"] not in {"WATCHING", "READY"}:
            raise ValueError("auto-rebuy watch is not active")
        if current_price_usd <= 0:
            raise ValueError("auto-rebuy observation price must be positive")
        lowest = min(float(watch["lowest_price_usd"]), current_price_usd)
        confirmations = (
            int(watch["confirmation_count"]) + 1 if qualifying else 0
        )
        status = (
            "READY"
            if qualifying and confirmations >= confirmation_required
            else "WATCHING"
        )
        self.connection.execute(
            """
            UPDATE auto_rebuy_watches
            SET status = ?, lowest_price_usd = ?, last_price_usd = ?,
                confirmation_count = ?, last_seen_at_epoch = ?,
                last_reason = ?, updated_at = ?
            WHERE chain = ? AND token_address = ?
              AND status IN ('WATCHING', 'READY')
            """,
            (
                status,
                lowest,
                current_price_usd,
                confirmations,
                observed_at_epoch,
                reason[:500],
                utc_now(),
                chain,
                token_address,
            ),
        )
        self.connection.commit()
        updated = self.load_auto_rebuy_watch(token_address, chain=chain)
        assert updated is not None
        return updated

    def reset_auto_rebuy_confirmation(
        self,
        *,
        token_address: str,
        reason: str,
        chain: str = "solana",
    ) -> None:
        self.connection.execute(
            "UPDATE auto_rebuy_watches SET status = 'WATCHING', "
            "confirmation_count = 0, last_reason = ?, updated_at = ? "
            "WHERE chain = ? AND token_address = ? "
            "AND status IN ('WATCHING', 'READY')",
            (reason[:500], utc_now(), chain, token_address),
        )
        self.connection.commit()

    def expire_auto_rebuy_watch(
        self,
        *,
        token_address: str,
        reason: str,
        chain: str = "solana",
    ) -> None:
        self.connection.execute(
            "UPDATE auto_rebuy_watches SET status = 'EXPIRED', "
            "confirmation_count = 0, last_reason = ?, updated_at = ? "
            "WHERE chain = ? AND token_address = ? "
            "AND status IN ('WATCHING', 'READY')",
            (reason[:500], utc_now(), chain, token_address),
        )
        self.connection.commit()

    def cancel_auto_rebuy(
        self, token_address: str, *, chain: str = "solana"
    ) -> bool:
        cursor = self.connection.execute(
            "UPDATE auto_rebuy_watches SET status = 'CANCELLED', "
            "confirmation_count = 0, last_reason = 'cancelled by operator', "
            "updated_at = ? WHERE chain = ? AND token_address = ? "
            "AND status IN ('WATCHING', 'READY', 'REVIEW')",
            (utc_now(), chain, token_address),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def freeze_auto_rebuy(
        self,
        *,
        token_address: str,
        error: str,
        chain: str = "solana",
    ) -> None:
        self.connection.execute(
            "UPDATE auto_rebuy_watches SET status = 'REVIEW', "
            "last_reason = ?, updated_at = ? "
            "WHERE chain = ? AND token_address = ? "
            "AND status IN ('WATCHING', 'READY')",
            (error[:500], utc_now(), chain, token_address),
        )
        self.connection.commit()

    def complete_auto_rebuy(
        self,
        *,
        token_address: str,
        buy_signature: str,
        reset_auto_sell: bool = False,
        chain: str = "solana",
    ) -> dict[str, Any]:
        if reset_auto_sell:
            holding = self.connection.execute(
                "SELECT entry_price, price_currency, cost_amount "
                "FROM owned_holdings WHERE chain = ? AND token_address = ?",
                (chain, token_address),
            ).fetchone()
            if (
                holding is None
                or holding["price_currency"] != "USD"
                or float(holding["entry_price"] or 0) <= 0
                or float(holding["cost_amount"] or 0) <= 0
            ):
                raise ValueError(
                    "auto-rebuy cannot reset selling without a USD cost basis"
                )
        now = utc_now()
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE auto_rebuy_watches
                SET status = 'BOUGHT',
                    completed_rebuys = completed_rebuys + 1,
                    buy_signature = ?, confirmation_count = 0,
                    last_reason = 'recovery buy confirmed', updated_at = ?
                WHERE chain = ? AND token_address = ?
                  AND status IN ('WATCHING', 'READY')
                """,
                (buy_signature, now, chain, token_address),
            )
            if cursor.rowcount != 1:
                raise ValueError("auto-rebuy watch is not active")
            if reset_auto_sell:
                self.connection.execute(
                    """
                    INSERT INTO auto_sell_policies(
                        chain, token_address, armed, stage,
                        last_signature, updated_at
                    ) VALUES (?, ?, 1, 0, NULL, ?)
                    ON CONFLICT(chain, token_address) DO UPDATE SET
                        armed = 1,
                        stage = 0,
                        last_signature = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (chain, token_address, now),
                )
        updated = self.load_auto_rebuy_watch(token_address, chain=chain)
        assert updated is not None
        return updated

    def auto_rebuy_status(self) -> dict[str, list[dict[str, Any]]]:
        watches = self.connection.execute(
            "SELECT * FROM auto_rebuy_watches "
            "ORDER BY updated_at DESC, token_address"
        ).fetchall()
        executions = self.connection.execute(
            "SELECT event_key, chain, token_address, symbol, funding_source, "
            "status, input_usdc_raw, expected_output_raw, actual_output_raw, "
            "output_decimals, signature, error, created_at, updated_at "
            "FROM auto_buy_executions WHERE event_key LIKE '%:auto-rebuy:%' "
            "ORDER BY updated_at DESC"
        ).fetchall()
        return {
            "watches": [dict(row) for row in watches],
            "executions": [dict(row) for row in executions],
        }

    def auto_buy_status(self) -> dict[str, Any]:
        policies = [
            dict(row)
            for row in self.connection.execute(
                "SELECT chain, token_address, symbol, armed, updated_at "
                "FROM auto_buy_policies ORDER BY symbol, token_address"
            ).fetchall()
        ]
        positions = [
            dict(row)
            for row in self.connection.execute(
                "SELECT chain, token_address, symbol, status, cost_usdc_raw, "
                "token_amount_raw, remaining_raw, token_decimals, "
                "realized_profit_usdc_raw, reinvest_credit_usdc_raw, "
                "buy_signature, opened_at, updated_at "
                "FROM auto_buy_positions ORDER BY id"
            ).fetchall()
        ]
        fund = dict(
            self.connection.execute(
                "SELECT seed_buys_used, reinvest_available_usdc_raw, "
                "realized_profit_usdc_raw, updated_at "
                "FROM auto_buy_fund WHERE id = 1"
            ).fetchone()
        )
        return {"fund": fund, "policies": policies, "positions": positions}

    def save_wallet_trade(
        self,
        *,
        wallet: str,
        signature: str,
        slot: int,
        mint: str,
        symbol: str | None,
        side: str,
        token_delta: float,
        native_sol_delta: float | None,
        observed_price_sol: float | None,
    ) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO wallet_trades(
                seen_at, wallet, signature, slot, mint, symbol, side,
                token_delta, native_sol_delta, observed_price_sol
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                wallet,
                signature,
                slot,
                mint,
                symbol,
                side,
                token_delta,
                native_sol_delta,
                observed_price_sol,
            ),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def trader_info(self, wallet: str) -> dict[str, Any]:
        totals = self.connection.execute(
            """
            SELECT
                COUNT(*) AS trades,
                SUM(CASE WHEN side = 'BUY' THEN 1 ELSE 0 END) AS buys,
                SUM(CASE WHEN side = 'SELL' THEN 1 ELSE 0 END) AS sells,
                COUNT(DISTINCT mint) AS unique_tokens,
                MIN(seen_at) AS first_seen,
                MAX(seen_at) AS last_seen
            FROM wallet_trades
            WHERE wallet = ?
            """,
            (wallet,),
        ).fetchone()
        recent_rows = self.connection.execute(
            """
            SELECT seen_at, signature, mint, symbol, side, token_delta,
                   native_sol_delta, observed_price_sol
            FROM wallet_trades
            WHERE wallet = ?
            ORDER BY id DESC
            LIMIT 10
            """,
            (wallet,),
        ).fetchall()
        return {
            "wallet": wallet,
            "trades": int(totals["trades"] or 0),
            "buys": int(totals["buys"] or 0),
            "sells": int(totals["sells"] or 0),
            "unique_tokens": int(totals["unique_tokens"] or 0),
            "first_seen": totals["first_seen"],
            "last_seen": totals["last_seen"],
            "recent": [dict(row) for row in recent_rows],
        }

    def save_wallet_event(
        self,
        *,
        chain: str,
        wallet: str,
        event_id: str,
        block_number: int | None,
        token_address: str,
        symbol: str | None,
        direction: str,
        token_amount: float,
        price_usd: float | None,
        source: str,
    ) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO wallet_events(
                seen_at, chain, wallet, event_id, block_number,
                token_address, symbol, direction, token_amount, price_usd,
                source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                chain,
                wallet,
                event_id,
                block_number,
                token_address,
                symbol,
                direction,
                token_amount,
                price_usd,
                source,
            ),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def multichain_wallet_info(self, wallet: str) -> dict[str, Any]:
        rows = self.connection.execute(
            """
            SELECT chain, COUNT(*) AS events,
                   COUNT(DISTINCT token_address) AS unique_tokens,
                   MIN(seen_at) AS first_seen, MAX(seen_at) AS last_seen
            FROM wallet_events
            WHERE lower(wallet) = lower(?)
            GROUP BY chain
            ORDER BY chain
            """,
            (wallet,),
        ).fetchall()
        recent = self.connection.execute(
            """
            SELECT seen_at, chain, event_id, token_address, symbol,
                   direction, token_amount, price_usd, source
            FROM wallet_events
            WHERE lower(wallet) = lower(?)
            ORDER BY id DESC
            LIMIT 25
            """,
            (wallet,),
        ).fetchall()
        return {
            "wallet": wallet,
            "chains": [dict(row) for row in rows],
            "recent": [dict(row) for row in recent],
        }

    def last_notification(
        self, provider: str, candidate_key: str
    ) -> tuple[str, float] | None:
        row = self.connection.execute(
            """
            SELECT decision, sent_at
            FROM notification_events
            WHERE provider = ? AND candidate_key = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (provider, candidate_key),
        ).fetchone()
        if row is None:
            return None
        return str(row["decision"]), float(row["sent_at"])

    def save_notification(
        self,
        *,
        provider: str,
        candidate_key: str,
        symbol: str,
        decision: str,
        sent_at: float,
        request_id: str | None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO notification_events(
                sent_at, provider, candidate_key, symbol, decision, request_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                sent_at,
                provider,
                candidate_key,
                symbol,
                decision,
                request_id,
            ),
        )
        self.connection.commit()

    def summary(self) -> dict[str, float | int]:
        row = self.connection.execute(
            """
            SELECT
                COUNT(*) AS total_positions,
                SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) AS open_positions,
                COALESCE(SUM(CASE WHEN status = 'CLOSED' THEN pnl_sol ELSE 0 END), 0)
                    AS realized_pnl_sol
            FROM positions
            """
        ).fetchone()
        return {
            "total_positions": int(row["total_positions"] or 0),
            "open_positions": int(row["open_positions"] or 0),
            "realized_pnl_sol": float(row["realized_pnl_sol"] or 0),
        }

    def close(self) -> None:
        self.connection.close()
