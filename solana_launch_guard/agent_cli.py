from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
from pathlib import Path

from openai import OpenAIError

from .agent_capital import CapitalBook
from .hunter_shadow_strategy import ShadowRecoveryPolicy, assess_entry, assess_exit
from .agents import (
    AgentCoordinator,
    AgentRecord,
    AgentRole,
    Arbitration,
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
    group.add_argument("--shadow-performance", action="store_true", help="show persisted hunter shadow-trade performance")
    group.add_argument(
        "--shadow-once",
        action="store_true",
        help="make one decision per agent from current read-only snapshots",
    )
    group.add_argument(
        "--shadow-portfolio-sell-once",
        action="store_true",
        help="review one priced sell recommendation with the portfolio agent only",
    )
    group.add_argument(
        "--shadow-core-once",
        action="store_true",
        help="review only Solana opportunities and priced sell guidance; skip copy trader",
    )
    group.add_argument(
        "--shadow-core-loop",
        action="store_true",
        help="run Hunter and Portfolio Manager every 60 seconds until Ctrl+C",
    )
    group.add_argument(
        "--live-test-preflight",
        action="store_true",
        help="simulate the one-time $5 mainnet canary; never broadcast",
    )
    group.add_argument(
        "--live-test-execute",
        action="store_true",
        help="rerun simulation and broadcast the one-time $5 mainnet canary",
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


def _priced_sell_signal(value: object) -> dict[str, object]:
    if not isinstance(value, list):
        return {}
    priorities = {"EXIT WARNING": 3, "PROTECT PROFIT": 2, "TAKE PARTIAL": 1}
    eligible = []
    for item in value:
        if not isinstance(item, dict) or item.get("chain") != "solana":
            continue
        if item.get("decision") not in priorities:
            continue
        try:
            price = float(item.get("current_price") or 0)
            amount = float(item.get("current_value_usd") or 0)
            liquidity = float(item.get("liquidity_usd") or 0)
        except (TypeError, ValueError):
            continue
        if all(map(math.isfinite, (price, amount, liquidity))) and price > 0 and amount >= 2 and liquidity > 0:
            eligible.append(item)
    return max(eligible, key=lambda x: (priorities[x["decision"]], float(x["current_value_usd"])), default={})


def _solana_opportunity(value: object) -> dict[str, object]:
    if not isinstance(value, list):
        return {}
    for item in value:
        if (
            isinstance(item, dict)
            and item.get("chain") == "solana"
            and item.get("decision") not in {"AVOID", None}
            and isinstance(item.get("mint"), str)
            and 32 <= len(item["mint"]) <= 44
        ):
            return item
    return {}


def _snapshot_is_fresh(snapshot: dict[str, object], *, now: float | None = None) -> bool:
    try:
        age = (time.time() if now is None else now) - float(snapshot.get("generated_at") or 0)
    except (TypeError, ValueError):
        return False
    return 0 <= age <= 15


def shadow_core_loop(book: CapitalBook, *, interval_seconds: int = 60) -> None:
    if interval_seconds < 60:
        raise ValueError("shadow loop interval must be at least 60 seconds")
    last_seen: tuple[object, object] | None = None
    try:
        while True:
            try:
                recommendations = _read_json(os.getenv("RECOMMENDATION_SNAPSHOT_PATH", "launch_guard_recommendations.json"))
                portfolio = _read_json(os.getenv("PORTFOLIO_SNAPSHOT_PATH", "launch_guard_portfolio.json"))
                fresh = (
                    _snapshot_is_fresh(recommendations) and bool(_solana_opportunity(recommendations.get("candidates")) or (recommendations.get("tracked_candidates") and book.load()["agents"]["hunter-v1"]["positions"])),
                    _snapshot_is_fresh(portfolio) and bool(
                        _priced_sell_signal(portfolio.get("signals"))
                        or portfolio.get("loss_sale_reviews")
                    ),
                )
                signature = (
                    recommendations.get("generated_at") if fresh[0] else None,
                    portfolio.get("generated_at") if fresh[1] else None,
                )
                if any(fresh) and signature != last_seen:
                    result = shadow_once(OpenAIProposalModel(), book, core_only=True)
                    print(json.dumps(result, indent=2), flush=True)
                    last_seen = signature
                elif not any(fresh):
                    print("No fresh eligible Hunter or Portfolio input; waiting.", flush=True)
            except (OpenAIError, ValueError, RuntimeError, OSError) as exc:
                message = friendly_api_error(exc) if isinstance(exc, OpenAIError) else str(exc)
                print(f"Shadow cycle skipped: {message}", flush=True)
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print("Shadow agents stopped.", flush=True)


def shadow_once(model: OpenAIProposalModel | None, book: CapitalBook, *, portfolio_sell_only: bool = False, core_only: bool = False) -> dict[str, object]:
    recommendations = _read_json(
        os.getenv("RECOMMENDATION_SNAPSHOT_PATH", "launch_guard_recommendations.json")
    )
    portfolio = _read_json(
        os.getenv("PORTFOLIO_SNAPSHOT_PATH", "launch_guard_portfolio.json")
    )
    copy_data = _read_json(
        os.getenv("AGENT_COPY_SIGNAL_PATH", "launch_guard_copy_signals.json")
    )
    recovery_policy = ShadowRecoveryPolicy.from_env()
    shadow_arbiter = RiskArbiter(RiskPolicy(max_order_usd=5, max_position_pct=100, max_open_positions=2))
    observed = recommendations.get("candidates", [])
    tracked = recommendations.get("tracked_candidates", [])
    fresh_quotes = {
        item["mint"]: item for item in (tracked + observed)
        if isinstance(item, dict) and item.get("chain") == "solana"
        and isinstance(item.get("mint"), str)
        and 0 <= time.time() - float(item.get("quoted_at") or 0) <= 15
    } if (_snapshot_is_fresh(recommendations) and isinstance(observed, list) and isinstance(tracked, list)) else {}
    candidate_reviews = {item["mint"]: assess_entry(fresh_quotes[item["mint"]], recovery_policy)
                         for item in observed if isinstance(item, dict) and item.get("mint") in fresh_quotes}
    shadow_reviews: list[dict[str, object]] = []
    if not portfolio_sell_only:
        # Existing position marks and exits use the same fresh, read-only quote
        # snapshot as entries; a missing quote can never silently sell a token.
        open_positions = (book.load() or {}).get("agents", {}).get("hunter-v1", {}).get("positions", {})
        for mint, old_position in list(open_positions.items()):
            quote = fresh_quotes.get(mint)
            if quote is None:
                shadow_reviews.append({"mint": mint, "state": "HOLD", "reasons": ["fresh matching quote unavailable"]})
                continue
            if old_position.get("price_currency") != quote.get("price_currency"):
                shadow_reviews.append({"mint": mint, "state": "HOLD", "reasons": ["quote currency differs from entry"]})
                continue
            price = float(quote.get("price") or 0)
            if not math.isfinite(price) or price <= 0:
                continue
            marked = book.mark_shadow_position("hunter-v1", mint, price)
            review = assess_exit(marked, quote, recovery_policy)
            review.update({"mint": mint, "entry_price": marked["entry_price"], "current_price": price})
            state = review["state"]
            if state in {"EXIT", "EMERGENCY_EXIT", "TAKE_PARTIAL"}:
                fraction = 1.0
                stage = state
                if state == "TAKE_PARTIAL":
                    if not marked.get("principal_recovered"):
                        fraction = min(1.0, float(marked["allocated_usd"]) / float(marked["current_value_usd"]))
                        stage = "PRINCIPAL_RECOVERY"
                    else:
                        fraction = recovery_policy.second_stage_fraction
                        stage = "SECOND_STAGE"
                sell_value = float(marked["current_value_usd"]) * fraction
                approval = shadow_arbiter.evaluate_shadow_exit(
                    sell_value,
                    RiskSnapshot(mode="shadow", liquidity_usd=float(quote.get("liquidity_usd") or 0),
                                 quote_age_seconds=max(0, time.time() - float(quote["quoted_at"])),
                                 quoted_price_impact_pct=float(quote.get("quoted_price_impact_pct") or 0)),
                )
                review["arbitration"] = {"approved": approval.approved, "approved_usd": approval.approved_usd,
                                         "reasons": list(approval.reasons)}
                if approval.approved and approval.approved_usd >= recovery_policy.min_sell_usd:
                    fraction = min(1.0, approval.approved_usd / float(marked["current_value_usd"]))
                    if state in {"EXIT", "EMERGENCY_EXIT"} and fraction < 1:
                        stage = "EXIT_CHUNK"
                    slippage = float(quote.get("estimated_slippage_pct") or 0)
                    if not math.isfinite(slippage) or not 0 <= slippage < 100:
                        slippage = 0
                    review["shadow_fill"] = book.close_shadow_position("hunter-v1", mint, fraction=fraction, stage=stage, slippage_pct=slippage)
                    review["remaining_position"] = book.load()["agents"]["hunter-v1"]["positions"].get(mint)
                else:
                    review["reasons"].append(f"shadow exit blocked or below ${recovery_policy.min_sell_usd:.2f} minimum")
            shadow_reviews.append(review)
    candidate = _solana_opportunity(recommendations.get("candidates")) if core_only and _snapshot_is_fresh(recommendations) else ({} if core_only else _first_dict(recommendations.get("candidates")))
    holding = _priced_sell_signal(portfolio.get("signals")) if (portfolio_sell_only or core_only) and _snapshot_is_fresh(portfolio) else ({} if portfolio_sell_only or core_only else _first_dict(portfolio.get("signals")))
    leader = _first_dict(copy_data.get("signals"))
    reviews = (
        [item for item in portfolio.get("loss_sale_reviews", []) if isinstance(item, dict)]
        if _snapshot_is_fresh(portfolio) and isinstance(portfolio.get("loss_sale_reviews"), list)
        else []
    )
    inputs = (
        ("hunter-v1", AgentRole.OPPORTUNITY_HUNTER, {
            "candidate": candidate,
            "watched_candidates": [item for item in recommendations.get("candidates", [])
                                   if isinstance(item, dict) and item.get("chain") == "solana"
                                   and 0 <= time.time() - float(item.get("quoted_at") or 0) <= 15][:20]
                                  if _snapshot_is_fresh(recommendations) and isinstance(recommendations.get("candidates"), list) else [],
            "loss_sale_reviews": reviews,
        }),
        ("portfolio-v1", AgentRole.PORTFOLIO_MANAGER,
         {"owned_position": holding,
          "all_holdings": portfolio.get("signals", []),
          "loss_sale_reviews": reviews}),
        ("copy-v1", AgentRole.COPY_TRADER, {"observed_leader_trade": leader}),
    )
    if portfolio_sell_only and not holding:
        return {"mode": "shadow", "live_execution": False, "agents": [], "reason": "no priced Solana sell recommendation is available", "capital": book.public_status()}
    if portfolio_sell_only:
        inputs = (inputs[1],)
    if core_only:
        # Review portfolio exits first: each model request consumes time, and
        # a sell quote may age out while Hunter evaluates a separate token.
        inputs = tuple(item for item in (inputs[1], inputs[0]) if any(item[2].values()))
        if not inputs:
            return {"mode": "shadow", "live_execution": False, "agents": [], "hunter_candidate_reviews": candidate_reviews, "hunter_position_reviews": shadow_reviews, "reason": "no eligible Solana opportunity or priced sell recommendation", "capital": book.public_status()}
    if model is None:
        raise ValueError("an agent model is required for available shadow inputs")
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
        account = {row.agent_id: row for row in book.accounts()}[agent_id]
        source = next((value for value in context.values() if isinstance(value, dict) and value), {})
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
                "strategy_research": {
                    "status": "RESEARCH_ONLY",
                    "lessons": [
                        "Repeated tops are a hypothesis; require a closed-candle support break.",
                        "A failed retest with bearish engulfing strengthens a bearish review.",
                        "Time near a peak and volatility can adjust review levels; elapsed time alone is not a sell signal.",
                        "Half-life decays old evidence; it does not predict token lifespan or rebound probability.",
                        "A breakout with volume permits add review, not automatic averaging or a buy.",
                    ],
                    "constraint": "Do not use unvalidated research as execution authority; existing risk checks govern trades.",
                },
                "capital": {
                    "cash_usd": account.cash_usd,
                    "equity_usd": account.equity_usd,
                    "maximum_order_usd": 5,
                    "maximum_open_positions": 2,
                },
                "recovery_reviews": candidate_reviews if role is AgentRole.OPPORTUNITY_HUNTER else {},
                "hunter_position_reviews": shadow_reviews if role is AgentRole.OPPORTUNITY_HUNTER else [],
                **context,
            },
            RiskSnapshot(
                mode="shadow",
                equity_usd=account.equity_usd,
                daily_realized_pnl_usd=book.daily_realized_pnl(agent_id),
                open_positions=account.open_positions,
                liquidity_usd=liquidity,
                quote_age_seconds=quote_age,
            ),
        )
        entry_review = candidate_reviews.get(proposal.mint) if role is AgentRole.OPPORTUNITY_HUNTER else None
        if role is AgentRole.OPPORTUNITY_HUNTER and proposal.action is TradeAction.BUY:
            selected = next((item for item in context.get("watched_candidates", [])
                             if item.get("mint") == proposal.mint), None)
            if selected is None:
                arbitration = Arbitration(False, 0, ("buy mint lacks a fresh watched quote",))
            else:
                entry_review = assess_entry(selected, recovery_policy)
                arbitration = coordinator.arbiter.evaluate(
                    proposal,
                    RiskSnapshot(
                        mode="shadow", equity_usd=account.equity_usd,
                        daily_realized_pnl_usd=book.daily_realized_pnl(agent_id),
                        open_positions=account.open_positions,
                        liquidity_usd=float(selected.get("liquidity_usd") or 0),
                        quote_age_seconds=max(0, time.time() - float(selected["quoted_at"])),
                    ),
                )
                if entry_review["state"] != "BUY_READY":
                    arbitration = Arbitration(False, 0, tuple(entry_review["reasons"]) + arbitration.reasons)
        if proposal.action is TradeAction.REBUY and not any(
            item.get("decision") == "REBUY REVIEW"
            and item.get("token_address") == proposal.mint
            for item in reviews
        ):
            arbitration = Arbitration(False, 0, ("no confirmed net-loss rebound review for this mint",))
        shadow_position = None
        shadow_fill = None
        if arbitration.approved and proposal.action is TradeAction.BUY:
            selected = fresh_quotes[proposal.mint]
            price = float(selected["price"])
            shadow_position = book.reserve_shadow_buy(
                agent_id=agent_id,
                mint=proposal.mint,
                symbol=str(selected.get("symbol") or proposal.mint[:8]),
                amount_usd=arbitration.approved_usd,
                entry_price=price,
                price_currency=str(selected.get("price_currency") or "UNKNOWN"),
                entry_liquidity_usd=float(selected.get("liquidity_usd") or 0),
            )
            shadow_fill = {"action": "BUY", "mint": proposal.mint, "amount_usd": arbitration.approved_usd}
        updated_account = {row.agent_id: row for row in book.accounts()}[agent_id]
        results.append(
            {
                "agent_id": agent_id,
                "role": role.value,
                "input_available": bool(source or reviews),
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
                "recovery_review": entry_review,
                "shadow_fill": shadow_fill,
                "shadow_balance": {
                    "cash_usd": updated_account.cash_usd,
                    "reserved_usd": updated_account.reserved_usd,
                    "equity_usd": updated_account.equity_usd,
                    "open_positions": updated_account.open_positions,
                },
            }
        )
    output = {
        "mode": "shadow",
        "live_execution": False,
        "generated_at": time.time(),
        "agents": results,
        "hunter_candidate_reviews": candidate_reviews,
        "hunter_position_reviews": shadow_reviews,
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
        elif args.shadow_performance:
            result = book.performance()
        elif args.shadow_once:
            model = OpenAIProposalModel()
            result = shadow_once(model, book)
        elif args.shadow_portfolio_sell_once:
            model = OpenAIProposalModel() if _priced_sell_signal(_read_json(os.getenv("PORTFOLIO_SNAPSHOT_PATH", "launch_guard_portfolio.json")).get("signals")) else None
            result = shadow_once(model, book, portfolio_sell_only=True)
        elif args.shadow_core_once:
            model = OpenAIProposalModel()
            result = shadow_once(model, book, core_only=True)
        elif args.shadow_core_loop:
            book.accounts()
            shadow_core_loop(book)
            return
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
