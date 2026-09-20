from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .agents import AgentRole


DEFAULT_AGENT_CAPITAL_USD = 30.0
DEFAULT_MAX_ORDER_USD = 5.0
DEFAULT_MAX_OPEN_POSITIONS = 2


@dataclass(frozen=True, slots=True)
class AgentCapital:
    agent_id: str
    role: AgentRole
    starting_capital_usd: float
    cash_usd: float
    reserved_usd: float
    realized_pnl_usd: float
    open_positions: int
    marked_value_usd: float | None = None

    @property
    def equity_usd(self) -> float:
        return self.cash_usd + (self.reserved_usd if self.marked_value_usd is None else self.marked_value_usd)


class CapitalBook:
    """Persistent shadow capital; it has no wallet or execution capability."""

    AGENTS = (
        ("hunter-v1", AgentRole.OPPORTUNITY_HUNTER),
        ("portfolio-v1", AgentRole.PORTFOLIO_MANAGER),
        ("copy-v1", AgentRole.COPY_TRADER),
    )

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self, capital_usd: float = DEFAULT_AGENT_CAPITAL_USD) -> dict[str, Any]:
        if not math.isfinite(capital_usd) or capital_usd <= 0:
            raise ValueError("agent starting capital must be positive and finite")
        existing = self.load()
        if existing is not None:
            configured = float(existing["starting_capital_per_agent_usd"])
            if not math.isclose(configured, capital_usd):
                raise ValueError(
                    "capital book already exists with a different allocation; "
                    "it was not overwritten"
                )
            return existing
        now = datetime.now(UTC).isoformat()
        payload: dict[str, Any] = {
            "version": 1,
            "mode": "shadow",
            "live_execution": False,
            "starting_capital_per_agent_usd": round(capital_usd, 2),
            "total_starting_capital_usd": round(capital_usd * len(self.AGENTS), 2),
            "created_at": now,
            "updated_at": now,
            "agents": {},
        }
        for agent_id, role in self.AGENTS:
            payload["agents"][agent_id] = {
                "role": role.value,
                "starting_capital_usd": round(capital_usd, 2),
                "cash_usd": round(capital_usd, 2),
                "reserved_usd": 0.0,
                "realized_pnl_usd": 0.0,
                "positions": {},
            }
        self._write(payload)
        return payload

    def load(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise ValueError(f"could not read agent capital book: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("mode") != "shadow":
            raise ValueError("agent capital book is invalid or not shadow mode")
        return payload

    def accounts(self) -> tuple[AgentCapital, ...]:
        payload = self.load()
        if payload is None:
            raise ValueError("initialize agent capital before requesting status")
        rows: list[AgentCapital] = []
        for agent_id, raw in payload["agents"].items():
            rows.append(
                AgentCapital(
                    agent_id=agent_id,
                    role=AgentRole(raw["role"]),
                    starting_capital_usd=float(raw["starting_capital_usd"]),
                    cash_usd=float(raw["cash_usd"]),
                    reserved_usd=float(raw["reserved_usd"]),
                    realized_pnl_usd=float(raw["realized_pnl_usd"]),
                    open_positions=len(raw["positions"]),
                    marked_value_usd=sum(
                        float(p.get("current_value_usd", p["allocated_usd"]))
                        for p in raw["positions"].values()
                    ),
                )
            )
        return tuple(rows)

    def reserve_shadow_buy(
        self,
        *,
        agent_id: str,
        mint: str,
        symbol: str,
        amount_usd: float,
        entry_price: float,
        price_currency: str,
        entry_liquidity_usd: float = 0,
        max_order_usd: float = DEFAULT_MAX_ORDER_USD,
        max_open_positions: int = DEFAULT_MAX_OPEN_POSITIONS,
    ) -> dict[str, Any]:
        payload = self.load()
        if payload is None:
            raise ValueError("initialize agent capital before shadow trading")
        if agent_id not in payload["agents"]:
            raise ValueError("unknown agent account")
        if not math.isfinite(amount_usd) or amount_usd <= 0:
            raise ValueError("shadow order amount must be positive and finite")
        if amount_usd > max_order_usd:
            raise ValueError("shadow order exceeds the $5 maximum")
        if not mint or not math.isfinite(entry_price) or entry_price <= 0:
            raise ValueError("shadow buy requires a mint and positive entry price")
        account = payload["agents"][agent_id]
        positions = account["positions"]
        if mint in positions:
            raise ValueError("agent already has an open position in this mint")
        if len(positions) >= max_open_positions:
            raise ValueError("agent already has two open positions")
        if amount_usd > float(account["cash_usd"]):
            raise ValueError("agent cash balance is below the approved amount")
        account["cash_usd"] = round(float(account["cash_usd"]) - amount_usd, 8)
        account["reserved_usd"] = round(float(account["reserved_usd"]) + amount_usd, 8)
        now = datetime.now(UTC).isoformat()
        positions[mint] = {
            "symbol": symbol,
            "allocated_usd": round(amount_usd, 8),
            "entry_price": entry_price,
            "price_currency": price_currency,
            "opened_at": now,
            "entry_time": now,
            "entry_value_usd": round(amount_usd, 8),
            "highest_price_since_entry": entry_price,
            "current_price": entry_price,
            "current_value_usd": round(amount_usd, 8),
            "return_pct": 0.0,
            "drawdown_from_post_entry_peak_pct": 0.0,
            "entry_liquidity_usd": entry_liquidity_usd,
            "principal_recovered": False,
            "second_stage_taken": False,
            "status": "SHADOW_OPEN",
        }
        payload["updated_at"] = datetime.now(UTC).isoformat()
        self._write(payload)
        return positions[mint]

    def mark_shadow_position(self, agent_id: str, mint: str, price: float) -> dict[str, Any]:
        if not math.isfinite(price) or price <= 0:
            raise ValueError("shadow mark requires a finite positive price")
        payload = self.load()
        if payload is None:
            raise ValueError("initialize agent capital first")
        position = payload["agents"][agent_id]["positions"][mint]
        entry = float(position["entry_price"])
        peak = max(entry, float(position.get("highest_price_since_entry", entry)), price)
        cost = float(position["allocated_usd"])
        position.update(
            highest_price_since_entry=peak, current_price=price,
            current_value_usd=round(cost * price / entry, 8),
            return_pct=round((price / entry - 1) * 100, 6),
            drawdown_from_post_entry_peak_pct=round((peak - price) / peak * 100, 6),
        )
        self._record_equity_drawdown(payload, agent_id)
        payload["updated_at"] = datetime.now(UTC).isoformat()
        self._write(payload)
        return dict(position)

    def close_shadow_position(
        self, agent_id: str, mint: str, *, fraction: float = 1.0,
        slippage_pct: float = 0.0, stage: str = "EXIT",
    ) -> dict[str, Any]:
        if not 0 < fraction <= 1 or not 0 <= slippage_pct < 100:
            raise ValueError("invalid shadow sell fraction or estimated slippage")
        payload = self.load()
        if payload is None:
            raise ValueError("initialize agent capital first")
        account = payload["agents"][agent_id]
        position = account["positions"][mint]
        if float(position.get("current_value_usd", position["allocated_usd"])) * fraction > DEFAULT_MAX_ORDER_USD + 1e-8:
            raise ValueError("shadow exit exceeds the $5 maximum")
        cost = float(position["allocated_usd"]) * fraction
        gross = float(position.get("current_value_usd", position["allocated_usd"])) * fraction
        proceeds = gross * (1 - slippage_pct / 100)
        pnl = proceeds - cost
        opened_at = position.get("entry_time", position["opened_at"])
        closed_at = datetime.now(UTC)
        held = max(0.0, (closed_at - datetime.fromisoformat(opened_at)).total_seconds())
        fill = {"mint": mint, "stage": stage, "entry_value_usd": round(cost, 8),
                "exit_value_usd": round(proceeds, 8), "realized_pnl_usd": round(pnl, 8),
                "return_pct": round(pnl / cost * 100, 6), "holding_seconds": held,
                "slippage_pct": slippage_pct, "closed_at": closed_at.isoformat(),
                "opened_at": opened_at, "position_closed": fraction == 1}
        account["cash_usd"] = round(float(account["cash_usd"]) + proceeds, 8)
        account["reserved_usd"] = round(max(0, float(account["reserved_usd"]) - cost), 8)
        account["realized_pnl_usd"] = round(float(account["realized_pnl_usd"]) + pnl, 8)
        account.setdefault("completed_trades", []).append(fill)
        if fraction == 1:
            del account["positions"][mint]
        else:
            position["allocated_usd"] = round(float(position["allocated_usd"]) - cost, 8)
            position["current_value_usd"] = round(float(position["current_value_usd"]) - gross, 8)
            if stage == "PRINCIPAL_RECOVERY":
                position["principal_recovered"] = True
            elif stage == "SECOND_STAGE":
                position["second_stage_taken"] = True
        self._record_equity_drawdown(payload, agent_id)
        payload["updated_at"] = closed_at.isoformat()
        self._write(payload)
        return fill

    @staticmethod
    def _record_equity_drawdown(payload: dict[str, Any], agent_id: str) -> None:
        account = payload["agents"][agent_id]
        equity = float(account["cash_usd"]) + sum(
            float(p.get("current_value_usd", p["allocated_usd"]))
            for p in account["positions"].values()
        )
        peak = max(float(account.get("equity_high_water_usd", account["starting_capital_usd"])), equity)
        account["equity_high_water_usd"] = peak
        account["maximum_drawdown_pct"] = max(
            float(account.get("maximum_drawdown_pct", 0)),
            (peak - equity) / peak * 100 if peak else 0,
        )

    def performance(self, agent_id: str = "hunter-v1") -> dict[str, Any]:
        payload = self.load()
        if payload is None:
            raise ValueError("initialize agent capital first")
        account = payload["agents"][agent_id]
        fills = account.get("completed_trades", [])
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for index, fill in enumerate(fills):
            key = (fill["mint"], fill.get("opened_at", f"legacy-{index}"))
            trade = grouped.setdefault(key, {"entry_value_usd": 0.0, "realized_pnl_usd": 0.0,
                                             "holding_seconds": 0, "slippage_total": 0.0,
                                             "fill_count": 0, "position_closed": False})
            trade["entry_value_usd"] += fill["entry_value_usd"]
            trade["realized_pnl_usd"] += fill["realized_pnl_usd"]
            trade["holding_seconds"] = max(trade["holding_seconds"], fill["holding_seconds"])
            trade["slippage_total"] += fill["slippage_pct"]
            trade["fill_count"] += 1
            trade["position_closed"] |= fill.get("position_closed", True)
        trades = [t for t in grouped.values() if t["position_closed"]]
        for trade in trades:
            trade["return_pct"] = trade["realized_pnl_usd"] / trade["entry_value_usd"] * 100
        wins = [t for t in trades if t["realized_pnl_usd"] > 0]
        losses = [t for t in trades if t["realized_pnl_usd"] < 0]
        avg_win = sum(t["return_pct"] for t in wins) / len(wins) if wins else 0
        avg_loss = -sum(t["return_pct"] for t in losses) / len(losses) if losses else 0
        n = len(trades)
        peak = float(account["starting_capital_usd"])
        equity = peak
        drawdown = 0.0
        for trade in trades:
            equity += trade["realized_pnl_usd"]
            peak = max(peak, equity)
            drawdown = max(drawdown, (peak - equity) / peak * 100)
        marked = sum(float(p.get("current_value_usd", p["allocated_usd"])) for p in account["positions"].values())
        cost = sum(float(p["allocated_usd"]) for p in account["positions"].values())
        profits = sum(t["realized_pnl_usd"] for t in wins)
        losses_usd = -sum(t["realized_pnl_usd"] for t in losses)
        return {"agent_id": agent_id, "mode": "shadow", "live_execution": False,
                "completed_trades": n, "winning_trades": len(wins), "losing_trades": len(losses),
                "win_rate_pct": round(len(wins) / n * 100, 4) if n else 0,
                "average_winning_trade_pct": round(avg_win, 4), "average_losing_trade_pct": round(avg_loss, 4),
                "realized_pnl_usd": round(float(account["realized_pnl_usd"]), 8),
                "unrealized_pnl_usd": round(marked - cost, 8),
                "maximum_drawdown_pct": round(max(drawdown, float(account.get("maximum_drawdown_pct", 0))), 4),
                "average_holding_seconds": round(sum(t["holding_seconds"] for t in trades) / n, 2) if n else 0,
                "average_simulated_slippage_pct": round(sum(t["slippage_total"] / t["fill_count"] for t in trades) / n, 4) if n else 0,
                "profit_factor": round(profits / losses_usd, 4) if losses_usd else None,
                "expectancy_pct": round(len(wins) / n * avg_win - len(losses) / n * avg_loss, 4) if n else 0,
                "evidence_sufficient": False,
                "slippage_note": "zero means no estimate was available; not a measured fill"}

    def daily_realized_pnl(self, agent_id: str, *, today: str | None = None) -> float:
        payload = self.load()
        if payload is None:
            raise ValueError("initialize agent capital first")
        date = today or datetime.now(UTC).date().isoformat()
        return sum(float(fill["realized_pnl_usd"]) for fill in
                   payload["agents"][agent_id].get("completed_trades", [])
                   if str(fill.get("closed_at", "")).startswith(date))

    def public_status(self) -> dict[str, Any]:
        payload = self.load()
        if payload is None:
            raise ValueError("initialize agent capital before requesting status")
        return {
            "mode": "shadow",
            "live_execution": False,
            "starting_capital_per_agent_usd": payload["starting_capital_per_agent_usd"],
            "total_starting_capital_usd": payload["total_starting_capital_usd"],
            "agents": [
                {
                    "agent_id": row.agent_id,
                    "role": row.role.value,
                    "cash_usd": row.cash_usd,
                    "reserved_usd": row.reserved_usd,
                    "equity_usd": row.equity_usd,
                    "realized_pnl_usd": row.realized_pnl_usd,
                    "open_positions": row.open_positions,
                }
                for row in self.accounts()
            ],
        }

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
