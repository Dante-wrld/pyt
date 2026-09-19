from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from openai import OpenAIError

from .agent_capital import CapitalBook
from .agents import (
    AgentCoordinator,
    AgentRecord,
    AgentRole,
    RiskArbiter,
    RiskPolicy,
    RiskSnapshot,
    TradeAction,
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


def friendly_api_error(exc: Exception) -> str:
    code = str(getattr(exc, "code", "") or "")
    message = str(exc).casefold()
    if code in {"credit_balance_exhausted", "insufficient_quota"} or any(
        phrase in message
        for phrase in ("no credits remaining", "credit balance", "insufficient_quota")
    ):
        return (
            "OpenAI API credits are exhausted. Add API credits at "
            "https://platform.openai.com/settings/organization/billing/ "
            "and then rerun this command. No trade was executed."
        )
    if "invalid_api_key" in code or "incorrect api key" in message:
        return "OPENAI_API_KEY was rejected. Replace it in .env and try again."
    return f"OpenAI API request failed: {exc}"


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
    group.add_argument(
        "--initialize-capital",
        type=float,
        metavar="USD_PER_AGENT",
        help="create three isolated shadow accounts without wallet access",
    )
    group.add_argument(
        "--capital-status",
        action="store_true",
        help="show shadow cash, reserved capital, and open-position counts",
    )
    group.add_argument(
        "--shadow-once",
        action="store_true",
        help="make one decision per agent from current read-only snapshots",
    )
    group.add_argument(
        "--live-test-preflight",
        action="store_true",
        help="simulate the one-time $1 mainnet canary; never broadcast",
    )
    group.add_argument(
        "--live-test-execute",
        action="store_true",
        help="rerun simulation and broadcast the one-time $1 mainnet canary",
    )
    parser.add_argument(
        "--confirm",
        help="required literal confirmation for --live-test-execute",
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


def _read_json(path: str) -> dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"could not read shadow input {path}: {exc}") from exc
    return value if isinstance(value, dict) else {}


def _first_dict(value: object) -> dict[str, object]:
    if isinstance(value, list):
        return next((item for item in value if isinstance(item, dict)), {})
    return {}


def shadow_once(model: OpenAIProposalModel, book: CapitalBook) -> dict[str, object]:
    recommendations = _read_json(
        os.getenv("RECOMMENDATION_SNAPSHOT_PATH", "launch_guard_recommendations.json")
    )
    portfolio = _read_json(
        os.getenv("PORTFOLIO_SNAPSHOT_PATH", "launch_guard_portfolio.json")
    )
    copy_data = _read_json(
        os.getenv("AGENT_COPY_SIGNAL_PATH", "launch_guard_copy_signals.json")
    )
    accounts = {row.agent_id: row for row in book.accounts()}
    candidate = _first_dict(recommendations.get("candidates"))
    holding = _first_dict(portfolio.get("signals"))
    leader = _first_dict(copy_data.get("signals"))
    inputs = (
        ("hunter-v1", AgentRole.OPPORTUNITY_HUNTER, {"candidate": candidate}),
        ("portfolio-v1", AgentRole.PORTFOLIO_MANAGER, {"owned_position": holding}),
        ("copy-v1", AgentRole.COPY_TRADER, {"observed_leader_trade": leader}),
    )
    coordinator = AgentCoordinator(
        model,
        RiskArbiter(
            RiskPolicy(
                max_order_usd=5,
                max_position_pct=100,
                max_open_positions=2,
            )
        ),
    )
    results: list[dict[str, object]] = []
    for agent_id, role, context in inputs:
        account = accounts[agent_id]
        source = next(iter(context.values()))
        liquidity = float(source.get("liquidity_usd") or 0) if source else 0
        generated = {
            AgentRole.OPPORTUNITY_HUNTER: recommendations.get("generated_at"),
            AgentRole.PORTFOLIO_MANAGER: portfolio.get("generated_at"),
            AgentRole.COPY_TRADER: copy_data.get("generated_at"),
        }[role]
        quote_age = max(0, time.time() - float(generated or time.time()))
        proposal, arbitration = coordinator.ask(
            AgentRecord(agent_id, role),
            {
                "mode": "shadow",
                "capital": {
                    "cash_usd": account.cash_usd,
                    "equity_usd": account.equity_usd,
                    "maximum_order_usd": 5,
                    "maximum_open_positions": 2,
                },
                **context,
            },
            RiskSnapshot(
                mode="shadow",
                equity_usd=account.equity_usd,
                open_positions=account.open_positions,
                liquidity_usd=liquidity,
                quote_age_seconds=quote_age,
            ),
        )
        shadow_position = None
        if arbitration.approved and proposal.action is TradeAction.BUY:
            price = float(source.get("price") or source.get("current_price") or 0)
            shadow_position = book.reserve_shadow_buy(
                agent_id=agent_id,
                mint=proposal.mint,
                symbol=str(source.get("symbol") or proposal.mint[:8]),
                amount_usd=arbitration.approved_usd,
                entry_price=price,
                price_currency=str(source.get("price_currency") or "UNKNOWN"),
            )
        results.append(
            {
                "agent_id": agent_id,
                "role": role.value,
                "input_available": bool(source),
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
                "shadow_position": shadow_position,
            }
        )
    output = {
        "mode": "shadow",
        "live_execution": False,
        "generated_at": time.time(),
        "agents": results,
        "capital": book.public_status(),
    }
    log_path = Path(os.getenv("AGENT_DECISION_LOG_PATH", "launch_guard_agent_decisions.jsonl"))
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(output, sort_keys=True) + "\n")
    return output


def main() -> None:
    args = build_parser().parse_args()
    load_dotenv()
    capital_path = os.getenv(
        "AGENT_CAPITAL_PATH", "launch_guard_agent_capital.json"
    )
    book = CapitalBook(capital_path)
    try:
        if args.initialize_capital is not None:
            book.initialize(args.initialize_capital)
            result = book.public_status()
        elif args.capital_status:
            result = book.public_status()
        elif args.shadow_once:
            model = OpenAIProposalModel()
            result = shadow_once(model, book)
        elif args.live_test_preflight or args.live_test_execute:
            from .agent_live_test import run_live_canary

            result = asyncio.run(
                run_live_canary(
                    execute=args.live_test_execute,
                    confirmation=args.confirm,
                )
            )
        else:
            model = OpenAIProposalModel()
            result = connection_test(model) if args.test_api else paper_demo(model)
    except OpenAIError as exc:
        raise SystemExit(friendly_api_error(exc)) from None
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
