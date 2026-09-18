from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from .config import Settings
from .portfolio import OwnedHolding


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chain, token_address)
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
                expected_output_raw INTEGER,
                signature TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

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
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO portfolio_states(
                chain, token_address, peak_price,
                baseline_liquidity_usd, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chain, token_address) DO UPDATE SET
                peak_price = excluded.peak_price,
                baseline_liquidity_usd = excluded.baseline_liquidity_usd,
                updated_at = excluded.updated_at
            """,
            (
                chain,
                token_address,
                peak_price,
                baseline_liquidity_usd,
                utc_now(),
            ),
        )
        self.connection.commit()

    def load_portfolio_states(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT chain, token_address, peak_price, baseline_liquidity_usd "
            "FROM portfolio_states"
        ).fetchall()
        return [dict(row) for row in rows]

    def arm_auto_sell(self, token_address: str, *, chain: str = "solana") -> None:
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
                updated_at = excluded.updated_at
            """,
            (chain, token_address, utc_now()),
        )
        self.connection.commit()

    def disarm_auto_sell(
        self, token_address: str, *, chain: str = "solana"
    ) -> None:
        self.connection.execute(
            "UPDATE auto_sell_policies SET armed = 0, updated_at = ? "
            "WHERE chain = ? AND token_address = ?",
            (utc_now(), chain, token_address),
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
    ) -> bool:
        now = utc_now()
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO auto_sell_executions(
                event_key, chain, token_address, symbol, stage, status,
                requested_raw, expected_output_raw, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)
            """,
            (
                event_key,
                chain,
                token_address,
                symbol,
                stage,
                requested_raw,
                expected_output_raw,
                now,
                now,
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

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

    def freeze_auto_sell_execution(self, *, event_key: str, error: str) -> None:
        self.connection.execute(
            "UPDATE auto_sell_executions SET status = 'REVIEW', error = ?, "
            "updated_at = ? WHERE event_key = ? AND status = 'PENDING'",
            (error[:500], utc_now(), event_key),
        )
        self.connection.commit()

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
