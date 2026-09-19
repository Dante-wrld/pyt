from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Protocol


class AgentRole(StrEnum):
    OPPORTUNITY_HUNTER = "opportunity_hunter"
    PORTFOLIO_MANAGER = "portfolio_manager"
    COPY_TRADER = "copy_trader"


class AgentStatus(StrEnum):
    INCUBATING = "incubating"
    ACTIVE = "active"
    PROBATION = "probation"
    RETIRED = "retired"


class TradeAction(StrEnum):
    BUY = "BUY"
    HOLD = "HOLD"
    TAKE_PARTIAL = "TAKE_PARTIAL"
    SELL = "SELL"
    WATCH = "WATCH"


@dataclass(frozen=True, slots=True)
class TradeProposal:
    agent_id: str
    role: AgentRole
    action: TradeAction
    mint: str
    requested_usd: float
    confidence: float
    thesis: str
    evidence: tuple[str, ...] = ()
    leader_wallet: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def validate(self) -> None:
        if not self.agent_id.strip():
            raise ValueError("agent_id is required")
        if self.action in {TradeAction.BUY, TradeAction.TAKE_PARTIAL, TradeAction.SELL}:
            if not self.mint.strip():
                raise ValueError("mint is required for a trade proposal")
            if not math.isfinite(self.requested_usd) or self.requested_usd <= 0:
                raise ValueError("requested_usd must be positive and finite")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if not self.thesis.strip():
            raise ValueError("thesis is required")
        if self.role is AgentRole.COPY_TRADER and self.action is TradeAction.BUY:
            if not self.leader_wallet:
                raise ValueError("copy-trader buys require an attributed leader wallet")


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    mode: str = "paper"
    equity_usd: float = 0
    daily_realized_pnl_usd: float = 0
    open_positions: int = 0
    current_position_usd: float = 0
    liquidity_usd: float = 0
    quoted_price_impact_pct: float = 0
    quote_age_seconds: float = 0
    kill_switch: bool = False
    mint_blocked: bool = False


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    allowed_modes: tuple[str, ...] = ("paper", "shadow")
    max_order_usd: float = 5
    max_position_pct: float = 3
    max_open_positions: int = 2
    max_daily_loss_pct: float = 3
    min_liquidity_usd: float = 5_000
    max_price_impact_pct: float = 5
    max_quote_age_seconds: float = 15
    minimum_confidence: float = 0.65


@dataclass(frozen=True, slots=True)
class Arbitration:
    approved: bool
    approved_usd: float
    reasons: tuple[str, ...]


class RiskArbiter:
    """Deterministic final authority. Agent text cannot override these rules."""

    def __init__(self, policy: RiskPolicy | None = None) -> None:
        self.policy = policy or RiskPolicy()

    def evaluate(self, proposal: TradeProposal, state: RiskSnapshot) -> Arbitration:
        proposal.validate()
        reasons: list[str] = []
        if state.kill_switch:
            reasons.append("kill switch is active")
        if state.mode not in self.policy.allowed_modes:
            reasons.append(f"mode {state.mode!r} is not enabled for agents")
        if state.mint_blocked:
            reasons.append("mint is blocked")
        if proposal.confidence < self.policy.minimum_confidence:
            reasons.append("confidence is below the policy minimum")
        if state.quote_age_seconds > self.policy.max_quote_age_seconds:
            reasons.append("market quote is stale")
        if abs(state.quoted_price_impact_pct) > self.policy.max_price_impact_pct:
            reasons.append("quoted price impact exceeds the policy limit")
        if proposal.action is TradeAction.BUY:
            if state.liquidity_usd < self.policy.min_liquidity_usd:
                reasons.append("liquidity is below the policy minimum")
            if state.open_positions >= self.policy.max_open_positions:
                reasons.append("maximum open positions reached")
            max_loss = state.equity_usd * self.policy.max_daily_loss_pct / 100
            if state.daily_realized_pnl_usd <= -max_loss and max_loss > 0:
                reasons.append("daily loss limit reached")

        if proposal.action not in {TradeAction.BUY, TradeAction.TAKE_PARTIAL, TradeAction.SELL}:
            return Arbitration(False, 0, ("proposal does not request execution",))
        if reasons:
            return Arbitration(False, 0, tuple(reasons))

        position_cap = state.equity_usd * self.policy.max_position_pct / 100
        remaining = max(0.0, position_cap - state.current_position_usd)
        approved = min(proposal.requested_usd, self.policy.max_order_usd)
        if proposal.action is TradeAction.BUY:
            approved = min(approved, remaining)
        if approved <= 0:
            return Arbitration(False, 0, ("position allocation is exhausted",))
        if approved < proposal.requested_usd:
            reasons.append(f"order capped from ${proposal.requested_usd:.2f} to ${approved:.2f}")
        reasons.append("passed deterministic risk checks")
        return Arbitration(True, round(approved, 2), tuple(reasons))


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    agent_id: str
    role: AgentRole
    mint: str
    realized_pnl_usd: float
    fees_usd: float = 0
    capital_deployed_usd: float = 0
    closed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def net_pnl_usd(self) -> float:
        return self.realized_pnl_usd - self.fees_usd


@dataclass(frozen=True, slots=True)
class Performance:
    trades: int
    wins: int
    losses: int
    net_pnl_usd: float
    return_pct: float
    max_drawdown_pct: float


def calculate_performance(entries: Iterable[LedgerEntry]) -> Performance:
    rows = list(entries)
    capital = sum(max(0, row.capital_deployed_usd) for row in rows)
    net_values = [row.net_pnl_usd for row in rows]
    running = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in net_values:
        running += value
        peak = max(peak, running)
        denominator = max(capital, 1.0)
        max_drawdown = max(max_drawdown, (peak - running) / denominator * 100)
    net = sum(net_values)
    return Performance(
        trades=len(rows),
        wins=sum(value > 0 for value in net_values),
        losses=sum(value < 0 for value in net_values),
        net_pnl_usd=round(net, 8),
        return_pct=round(net / capital * 100, 4) if capital else 0,
        max_drawdown_pct=round(max_drawdown, 4),
    )


@dataclass(slots=True)
class AgentRecord:
    agent_id: str
    role: AgentRole
    generation: int = 1
    status: AgentStatus = AgentStatus.INCUBATING
    born_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    probation_weeks: int = 0


@dataclass(frozen=True, slots=True)
class SurvivalPolicy:
    minimum_age_days: int = 28
    minimum_trades: int = 30
    probation_weeks_before_retirement: int = 2
    probation_return_pct: float = -2
    retirement_return_pct: float = -5
    maximum_drawdown_pct: float = 15


@dataclass(frozen=True, slots=True)
class SurvivalDecision:
    status: AgentStatus
    reason: str


class SurvivalEvaluator:
    def __init__(self, policy: SurvivalPolicy | None = None) -> None:
        self.policy = policy or SurvivalPolicy()

    def evaluate(
        self,
        record: AgentRecord,
        performance: Performance,
        *,
        now: datetime | None = None,
    ) -> SurvivalDecision:
        age = (now or datetime.now(UTC)) - record.born_at
        if age < timedelta(days=self.policy.minimum_age_days):
            return SurvivalDecision(AgentStatus.INCUBATING, "minimum evaluation age not reached")
        if performance.trades < self.policy.minimum_trades:
            return SurvivalDecision(AgentStatus.INCUBATING, "minimum completed trades not reached")
        failing = (
            performance.return_pct <= self.policy.probation_return_pct
            or performance.max_drawdown_pct >= self.policy.maximum_drawdown_pct
        )
        severe = performance.return_pct <= self.policy.retirement_return_pct
        if failing and (severe or record.probation_weeks >= self.policy.probation_weeks_before_retirement):
            return SurvivalDecision(AgentStatus.RETIRED, "persistent poor risk-adjusted performance")
        if failing:
            return SurvivalDecision(AgentStatus.PROBATION, "weekly performance breached probation limits")
        return SurvivalDecision(AgentStatus.ACTIVE, "performance remains inside survival limits")


def daily_report(entries: Iterable[LedgerEntry], report_date: date) -> dict[str, Any]:
    day_rows = [row for row in entries if row.closed_at.date() == report_date]
    by_agent: dict[str, dict[str, Any]] = {}
    for row in day_rows:
        item = by_agent.setdefault(row.agent_id, {"role": row.role.value, "entries": []})
        item["entries"].append(row)
    agents: dict[str, Any] = {}
    for agent_id, item in by_agent.items():
        perf = calculate_performance(item.pop("entries"))
        agents[agent_id] = {"role": item["role"], **performance_dict(perf)}
    total = calculate_performance(day_rows)
    return {"date": report_date.isoformat(), "total": performance_dict(total), "agents": agents}


def performance_dict(value: Performance) -> dict[str, int | float]:
    return {
        "trades": value.trades,
        "wins": value.wins,
        "losses": value.losses,
        "net_pnl_usd": value.net_pnl_usd,
        "return_pct": value.return_pct,
        "max_drawdown_pct": value.max_drawdown_pct,
    }


class AgentModel(Protocol):
    def propose(self, *, role: AgentRole, context: dict[str, Any]) -> dict[str, Any]: ...


ROLE_INSTRUCTIONS: dict[AgentRole, str] = {
    AgentRole.OPPORTUNITY_HUNTER: "Find new candidates; never manage existing positions or execute trades.",
    AgentRole.PORTFOLIO_MANAGER: "Manage owned positions and exits; never discover copy-trade leaders.",
    AgentRole.COPY_TRADER: "Discover and score traders, then selectively propose attributed copies; never blindly mirror.",
}


class AgentCoordinator:
    """Model adapter that accepts only validated, structured proposals."""

    def __init__(self, model: AgentModel, arbiter: RiskArbiter | None = None) -> None:
        self.model = model
        self.arbiter = arbiter or RiskArbiter()

    def ask(
        self,
        record: AgentRecord,
        context: dict[str, Any],
        risk: RiskSnapshot,
    ) -> tuple[TradeProposal, Arbitration]:
        safe_context = {**context, "instruction": ROLE_INSTRUCTIONS[record.role]}
        raw = self.model.propose(role=record.role, context=safe_context)
        proposal = TradeProposal(
            agent_id=record.agent_id,
            role=record.role,
            action=TradeAction(str(raw["action"]).upper()),
            mint=str(raw.get("mint", "")),
            requested_usd=float(raw.get("requested_usd", 0)),
            confidence=float(raw.get("confidence", 0)),
            thesis=str(raw.get("thesis", "")),
            evidence=tuple(str(item) for item in raw.get("evidence", [])),
            leader_wallet=raw.get("leader_wallet"),
        )
        return proposal, self.arbiter.evaluate(proposal, risk)


class JsonLedger:
    """Optional local paper ledger; callers choose the path and keep it out of git."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, entry: LedgerEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "agent_id": entry.agent_id,
            "role": entry.role.value,
            "mint": entry.mint,
            "realized_pnl_usd": entry.realized_pnl_usd,
            "fees_usd": entry.fees_usd,
            "capital_deployed_usd": entry.capital_deployed_usd,
            "closed_at": entry.closed_at.isoformat(),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
