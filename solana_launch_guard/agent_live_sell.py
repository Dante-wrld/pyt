"""One real Portfolio-agent exit, with a durable one-decision session limit."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import math
import os
import re
import time
from pathlib import Path

from .agent_live_test import CanaryJournal
from .agents import AgentRole, TradeAction, TradeProposal

SESSION_PATH = "launch_guard_agent_sell_session.json"


def require_live_flags():
    if os.getenv("AGENT_LIVE_TEST_ENABLED", "false").lower() != "true":
        raise ValueError("AGENT_LIVE_TEST_ENABLED must be true")
    if os.getenv("AGENT_LIVE_KILL_SWITCH", "true").lower() != "false":
        raise ValueError("AGENT_LIVE_KILL_SWITCH is active")
    # Avoid simultaneous broad deterministic execution in this test session.
    if any(os.getenv(name, "false").lower() in {"true", "1", "yes", "on"}
           for name in ("AUTO_BUY_LIVE", "AUTO_SELL_LIVE")):
        raise ValueError("For this isolated test set AUTO_BUY_LIVE=false and AUTO_SELL_LIVE=false; this command executes its own guarded sale")


def eligible(snapshot, now):
    try:
        age = now - float(snapshot.get("generated_at", 0))
        if not math.isfinite(age) or not 0 <= age <= 15:
            return []
    except (TypeError, ValueError, AttributeError):
        return []
    rows = snapshot.get("signals")
    if not isinstance(rows, list):
        return []
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mint = row.get("token_address", "")
        if (row.get("chain") != "solana" or row.get("decision") != "EXIT WARNING"
            or not isinstance(mint, str) or not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", mint)):
            continue
        try:
            value = float(row.get("current_value_usd") or 0)
            price = float(row.get("current_price") or 0)
            liquidity = float(row.get("liquidity_usd") or 0)
            if all(math.isfinite(x) for x in (value, price, liquidity)) and 2 <= value <= 5 and price > 0 and liquidity > 0:
                result.append(row)
        except (ValueError, TypeError):
            continue
    return result


def read_candidates():
    path = Path(os.getenv("PORTFOLIO_SNAPSHOT_PATH", "launch_guard_portfolio.json"))
    try:
        snapshot = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return eligible(snapshot, time.time())


def validate_choice(raw, candidates):
    proposal = TradeProposal(
        agent_id="portfolio-v1", role=AgentRole.PORTFOLIO_MANAGER,
        action=TradeAction(str(raw["action"]).upper()), mint=str(raw.get("mint", "")),
        requested_usd=float(raw.get("requested_usd", 0)), confidence=float(raw.get("confidence", 0)),
        thesis=str(raw.get("thesis", "")),
    )
    proposal.validate()
    if proposal.action is not TradeAction.SELL:
        return None
    if proposal.confidence < 0.65 or not 2 <= proposal.requested_usd <= 5:
        raise ValueError("Agent sale does not meet confidence or $2-$5 amount limits")
    row = next((r for r in candidates if r["token_address"] == proposal.mint), None)
    if row is None:
        raise ValueError("Agent mint lacks a fresh eligible full-exit recommendation")
    if abs(proposal.requested_usd - float(row["current_value_usd"])) > 0.05:
        raise ValueError("Only full-position exits are supported in this test")
    return proposal


async def run():
    require_live_flags()
    journal = CanaryJournal(SESSION_PATH)
    journal.assert_unused()
    candidates = read_candidates()[:10]
    if not candidates:
        return {"mode": "live-agent-sell", "broadcast": False, "status": "NO_ELIGIBLE_EXIT", "model_requests": 0}
    # Verify local execution dependencies and signer before spending model credits.
    from .config import Settings
    from .core import SQLiteStore
    from .execution import KeyringSolanaSigner
    from .openai_agents import OpenAIProposalModel
    from .app import execute_owned_sell_once, verify_owned_sell
    settings = replace(Settings.from_env(), auto_sell_max_price_impact_pct=3.0,
                       auto_sell_max_slippage_bps=300, portfolio_min_sell_value_usd=2.0)
    if not settings.solana_wallet_address or not settings.jupiter_api_key:
        raise ValueError("Solana wallet and Jupiter API key are required")
    signer = KeyringSolanaSigner(expected_public_key=settings.solana_wallet_address)
    signer.public_key
    model = OpenAIProposalModel()
    model.client = model.client.with_options(max_retries=0)
    journal.claim({"status": "DECISION_STARTED", "at": time.time(), "agent": "portfolio-v1"})
    try:
        raw = model.propose(role=AgentRole.PORTFOLIO_MANAGER, context={
            "mode": "live_one_shot", "owned_exit_candidates": candidates[:10],
            "constraints": "Choose at most one listed full-position SELL, or HOLD. No partial sells, buys or rebuys. requested_usd must equal current_value_usd. Maximum $5 proceeds; minimum $2. You may decline every trade. Confidence is a model assessment, not a probability of profit.",
        })
        proposal = validate_choice(raw, candidates)
        journal.record({"status": "PROPOSED", "proposal": raw, "at": time.time()})
        if proposal is None:
            journal.record({"status": "NO_TRADE", "at": time.time()})
            return {"mode": "live-agent-sell", "broadcast": False, "status": "NO_TRADE", "proposal": raw}
        def guard():
            require_live_flags()
            # Refresh eligibility after model latency and again after simulation.
            fresh = read_candidates()
            if not any(r["token_address"] == proposal.mint for r in fresh):
                raise ValueError("Exit recommendation expired or changed; no sale submitted")
        guard()
        store = SQLiteStore(settings.database_path)
        try:
            sale = await execute_owned_sell_once(settings, store, proposal.mint, proposal.mint, before_broadcast=guard)
            journal.record({"status": "SALE_REPORTED", "sale": sale, "at": time.time()})
            verified = await verify_owned_sell(settings, store, proposal.mint)
        finally:
            store.close()
        journal.record({"status": "VERIFIED", "verification": verified, "at": time.time()})
        return {"mode": "live-agent-sell", "broadcast": True, "status": "VERIFIED", "proposal": raw, "sale": sale, "verification": verified}
    except Exception:
        # The durable claim remains, even for an ambiguous failure. Never auto-retry.
        journal.record({"status": "STOPPED_REVIEW_REQUIRED", "at": time.time()})
        raise RuntimeError("Agent session stopped; inspect local session and sale journals before any retry. The session will not execute again automatically.") from None


def main():
    import argparse
    from .config import _load_dotenv
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True, help="authorize one agent-selected real Solana full exit ($2-$5)")
    parser.parse_args()
    _load_dotenv()
    try:
        print(json.dumps(asyncio.run(run()), indent=2))
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
