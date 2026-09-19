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

    @property
    def equity_usd(self) -> float:
        return self.cash_usd + self.reserved_usd


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
        if not mint or entry_price <= 0:
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
        positions[mint] = {
            "symbol": symbol,
            "allocated_usd": round(amount_usd, 8),
            "entry_price": entry_price,
            "price_currency": price_currency,
            "opened_at": datetime.now(UTC).isoformat(),
            "status": "SHADOW_OPEN",
        }
        payload["updated_at"] = datetime.now(UTC).isoformat()
        self._write(payload)
        return positions[mint]

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
