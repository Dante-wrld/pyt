"""wide-v1: a paper-only book that trades what the freeze keeps out.

The live strategy is frozen to BUY ZONE on tokens 3+ days old, so hunter-v1
rarely trades and evidence for anything else arrives slowly. This book
takes every BUY_READY signal of the kinds in WIDE_DECISIONS (default BUY
ZONE, BUY NOW, EARLY BUY; MOMENTUM BUY stays out while it is paused) at any
token age, with hunter-v1's own exits, so more trades come in without
changing a single live rule. It never touches real money and never calls
the model.

Each position records its signal type and, if the live gate would have
refused it, why (`live_blocked`), so `launch-guard-eval books` can show the
trades live would also have taken separately from the ones only this book
takes. Still applies: the entry review (assess_entry: pullback, momentum,
liquidity, volume, score, confirmations), the shared re-entry rule, $5 per
position, 4 open positions, the same 1.2% round-trip cost as the other
research books, and a $3 daily loss limit.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from .agent_capital import CapitalBook
from .agents import AgentRole
from .hunter_shadow_strategy import (
    ShadowRecoveryPolicy,
    assess_entry,
    assess_exit,
    reentry_block_reason,
)
from .live_trial_ledger import principal_sale_fraction
from .swing_strategy import (
    QuoteFetcher,
    _fresh_board,
    _read_snapshot,
    dexscreener_quotes,
)

LOGGER = logging.getLogger("solana_launch_guard.wide")

WIDE_AGENT_ID = "wide-v1"
DEFAULT_WIDE_BOOK = "launch_guard_wide_capital.json"
DEFAULT_WIDE_DECISIONS = ("BUY ZONE", "BUY NOW", "EARLY BUY")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class WideCapitalBook(CapitalBook):
    AGENTS: tuple[tuple[str, AgentRole], ...] = (
        (WIDE_AGENT_ID, AgentRole.OPPORTUNITY_HUNTER),
    )


def wide_decisions() -> tuple[str, ...]:
    raw = os.getenv("WIDE_DECISIONS", "")
    chosen = tuple(d.strip().upper() for d in raw.split(",") if d.strip())
    return chosen or DEFAULT_WIDE_DECISIONS


async def wide_cycle(
    book: CapitalBook, snapshot: dict[str, Any], *,
    policy: ShadowRecoveryPolicy,
    decisions: Sequence[str] = DEFAULT_WIDE_DECISIONS,
    max_open_positions: int = 4,
    order_usd: float = 5.0,
    max_daily_loss_usd: float = 3.0,
    round_trip_cost_pct: float = 1.2,
    fetch_quotes: QuoteFetcher = dexscreener_quotes,
    now: float | None = None,
    agent_id: str = WIDE_AGENT_ID,
) -> dict[str, Any]:
    """One paper cycle: exit held positions with hunter-v1's rules, then buy
    at most one BUY_READY candidate. `agent_id` lets another paper book
    (momentum-take-v1) reuse the same cycle under its own name."""
    from .live_trial_runner import live_entry_gate
    from .strategy_profile import StrategyProfile

    at = time.time() if now is None else now
    fresh, observed = _fresh_board(snapshot, at)
    payload = book.load() or {}
    positions = dict(payload.get("agents", {}).get(agent_id, {})
                     .get("positions", {}))
    off_board = [m for m in positions if m not in fresh]
    fetched: dict[str, dict[str, Any]] = {}
    if off_board:
        try:
            fetched = await fetch_quotes(off_board)
        except Exception as exc:  # noqa: BLE001 - a quote outage must not stop the loop
            LOGGER.warning("wide: off-board quotes failed: %s", exc)
    exits: list[dict[str, Any]] = []
    for mint, position in positions.items():
        quote = fresh.get(mint) or fetched.get(mint)
        if quote is None or str(quote.get("price_currency") or "USD") != str(
                position.get("price_currency") or "USD"):
            exits.append({"mint": mint, "state": "HOLD",
                          "reasons": ["no comparable quote"]})
            continue
        price = float(quote.get("price") or 0)
        if not math.isfinite(price) or price <= 0:
            continue
        marked = book.mark_shadow_position(agent_id, mint, price)
        review = assess_exit(marked, quote, policy, now=at)
        review["mint"] = mint
        state = review["state"]
        if state in {"EXIT", "EMERGENCY_EXIT"}:
            fraction, stage = 1.0, "EXIT"
        elif state == "TAKE_PARTIAL":
            if not marked.get("principal_recovered"):
                fraction = principal_sale_fraction(
                    float(marked["allocated_usd"]), float(marked["current_value_usd"]))
                stage = "PRINCIPAL_RECOVERY"
            else:
                fraction, stage = policy.second_stage_fraction, "SECOND_STAGE"
        else:
            exits.append(review)
            continue
        review["fill"] = book.close_shadow_position(
            agent_id, mint, fraction=fraction, stage=stage,
            slippage_pct=round_trip_cost_pct)
        exits.append(review)

    entry: dict[str, Any] | None = None
    payload = book.load() or {}
    account = payload.get("agents", {}).get(agent_id, {})
    held = set(account.get("positions", {}))
    daily = book.daily_realized_pnl(agent_id)
    if (len(held) < max_open_positions
            and float(account.get("cash_usd", 0)) >= order_usd
            and daily > -max_daily_loss_usd):
        profile = StrategyProfile.from_env()
        ready: list[tuple[dict[str, Any], str | None]] = []
        for candidate in observed:
            mint = candidate["mint"]
            if mint in held or str(candidate.get("decision")) not in decisions:
                continue
            review = assess_entry(candidate, policy)
            if review["state"] != "BUY_READY":
                continue
            currency = str(candidate.get("price_currency") or "")
            if reentry_block_reason(
                book.sell_history(agent_id, mint, price_currency=currency),
                float(candidate.get("price") or 0),
            ) is not None:
                continue
            # Recorded, not enforced: would the live gate have taken it?
            gated = live_entry_gate(candidate, review, now=at, profile=profile)
            live_blocked = (None if gated["state"] == "BUY_READY"
                            else "; ".join(gated["reasons"]))
            ready.append((candidate, live_blocked))
        if ready:
            pick, live_blocked = max(
                ready, key=lambda item: float(item[0].get("signal_score") or 0))
            book.reserve_shadow_buy(
                agent_id=agent_id, mint=pick["mint"],
                symbol=str(pick.get("symbol") or pick["mint"][:6]),
                amount_usd=order_usd, entry_price=float(pick["price"]),
                price_currency=str(pick.get("price_currency") or "UNKNOWN"),
                entry_liquidity_usd=float(pick.get("liquidity_usd") or 0),
                max_open_positions=max_open_positions,
                pair_address=pick.get("pair_address"),
                pair_created_at_ms=pick.get("pair_created_at_ms"),
                decision=str(pick.get("decision")),
                live_blocked=live_blocked,
            )
            entry = {"mint": pick["mint"], "symbol": pick.get("symbol"),
                     "decision": pick.get("decision"), "price": pick["price"],
                     "live_blocked": live_blocked}
    return {"agent_id": agent_id, "mode": "shadow", "live_execution": False,
            "exits": exits, "entry": entry, "daily_realized_pnl_usd": daily,
            "at": datetime.fromtimestamp(at, UTC).isoformat()}


def main(argv: Sequence[str] | None = None) -> None:
    from .eval_cli import _load_dotenv

    _load_dotenv()
    parser = argparse.ArgumentParser(
        prog="launch-guard-wide",
        description="wide-v1: paper-only book for signals the freeze keeps out "
        "(never trades real money)")
    parser.add_argument(
        "--book", default=os.getenv("WIDE_BOOK_PATH", DEFAULT_WIDE_BOOK))
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    book = WideCapitalBook(args.book)
    book.initialize(30)
    if args.status:
        payload = book.load() or {}
        open_positions = payload.get("agents", {}).get(WIDE_AGENT_ID, {}).get(
            "positions", {})
        print(json.dumps({**book.performance(WIDE_AGENT_ID), "open": open_positions},
                         indent=2))
        return
    policy = ShadowRecoveryPolicy.from_env()
    decisions = wide_decisions()
    max_open = int(_env_float("WIDE_MAX_OPEN_POSITIONS", 4))
    snapshot_path = os.getenv("RECOMMENDATION_SNAPSHOT_PATH",
                              "launch_guard_recommendations.json")
    LOGGER.warning("wide-v1 (paper only): %s, %d open positions max",
                   ", ".join(decisions), max_open)
    while True:
        try:
            result = asyncio.run(wide_cycle(
                book, _read_snapshot(snapshot_path), policy=policy,
                decisions=decisions, max_open_positions=max_open))
            for review in result["exits"]:
                if "fill" in review:
                    LOGGER.info("WIDE SELL %s %s %s", review["mint"][:8],
                                review["state"], review["reasons"][-1])
            if result["entry"]:
                entry = result["entry"]
                LOGGER.info("WIDE BUY %s %s @ %s%s", entry["symbol"], entry["decision"],
                            entry["price"],
                            " (live would skip)" if entry["live_blocked"] else "")
        except (ValueError, OSError, RuntimeError) as exc:
            LOGGER.warning("wide cycle skipped: %s", exc)
        if args.once:
            return
        time.sleep(max(args.interval, 30.0))


if __name__ == "__main__":
    main()
