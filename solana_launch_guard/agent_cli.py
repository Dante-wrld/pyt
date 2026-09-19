from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .agents import (
    AgentCoordinator,
    AgentRecord,
    AgentRole,
    RiskSnapshot,
)
from .openai_agents import OpenAIProposalModel, connection_test


def load_dotenv(path: str = ".env") -> None:
    file_path = Path(path)
    if not file_path.exists():
        return
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip():
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Launch Guard AI-agent connection and paper-only tests."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--test-api",
        action="store_true",
        help="make one synthetic HOLD request; never access a wallet or execute",
    )
    group.add_argument(
        "--paper-demo",
        action="store_true",
        help="ask all three agents about synthetic data and arbitrate in paper mode",
    )
    return parser


def paper_demo(model: OpenAIProposalModel) -> dict[str, object]:
    coordinator = AgentCoordinator(model)
    scenarios = (
        (
            AgentRecord("hunter-v1", AgentRole.OPPORTUNITY_HUNTER),
            {
                "mode": "paper",
                "market": {
                    "mint": "SYNTHETIC_MINT",
                    "liquidity_usd": 25_000,
                    "price_change_m5_pct": 3,
                    "buys_m5": 30,
                    "sells_m5": 20,
                },
            },
        ),
        (
            AgentRecord("portfolio-v1", AgentRole.PORTFOLIO_MANAGER),
            {
                "mode": "paper",
                "owned_position": {
                    "mint": "SYNTHETIC_MINT",
                    "value_usd": 5,
                    "unrealized_pnl_pct": 8,
                },
            },
        ),
        (
            AgentRecord("copy-v1", AgentRole.COPY_TRADER),
            {
                "mode": "paper",
                "observed_leader_trade": {
                    "leader_wallet": "SYNTHETIC_PUBLIC_WALLET",
                    "mint": "SYNTHETIC_MINT",
                    "price_move_since_entry_pct": 1,
                },
            },
        ),
    )
    risk = RiskSnapshot(
        mode="paper",
        equity_usd=500,
        liquidity_usd=25_000,
        quoted_price_impact_pct=1,
    )
    results: list[dict[str, object]] = []
    for record, context in scenarios:
        proposal, arbitration = coordinator.ask(record, context, risk)
        results.append(
            {
                "agent_id": record.agent_id,
                "role": record.role.value,
                "proposal": {
                    "action": proposal.action.value,
                    "mint": proposal.mint,
                    "requested_usd": proposal.requested_usd,
                    "confidence": proposal.confidence,
                    "thesis": proposal.thesis,
                    "evidence": list(proposal.evidence),
                    "leader_wallet": proposal.leader_wallet,
                },
                "arbitration": {
                    "approved": arbitration.approved,
                    "approved_usd": arbitration.approved_usd,
                    "reasons": list(arbitration.reasons),
                },
            }
        )
    return {"mode": "paper", "live_execution": False, "agents": results}


def main() -> None:
    args = build_parser().parse_args()
    load_dotenv()
    try:
        model = OpenAIProposalModel()
        result = connection_test(model) if args.test_api else paper_demo(model)
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
