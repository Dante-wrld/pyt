"""wide-fresh-v1: wide-v1 with a fresh-setup re-entry gate.

wide-v1 (wide_shadow.py) re-bought the same token on the same signal type as
soon as it re-qualified. For a token chopping sideways in a tight range,
"re-qualifies" can mean "reclaims a nearby exit price within minutes" - the
existing shared re-entry rule (reentry_block_reason) only requires price to
reclaim the entry of a losing streak, which is a low bar inside a narrow
range. Found 2026-09-28: wide-v1 bought and stagnation-exited the same token
(HYPE) 28 times in 30 closed trades, each a small loss, because its price
never actually left an ~1.5%-wide band all day.

This variant keeps every other wide-v1 rule (entry review, cost, size, open
positions, daily loss limit) and adds one restriction: after a position on a
mint closes, that mint may not be re-bought on the *same* decision type
until it makes a new high above the peak reached during the closed position,
then pulls back into the entry review's zone again. Chopping back into the
old range does not count as a new setup. The reason a mint is currently
gated is recorded (`fresh_setup_reason`) so it can be read back, not just
enforced silently.

wide-v1's own book and results are untouched by this file - it is a
separate agent, separate capital book, run as its own process, so the two
can be compared afterwards (by trade and by distinct token; see
launch-guard-eval books).
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
from .wide_shadow import (
    DEFAULT_WIDE_DECISIONS,
    paper_daily_loss_usd,
    paper_order_usd,
    wide_decisions,
)

LOGGER = logging.getLogger("solana_launch_guard.wide_fresh")

WIDE_FRESH_AGENT_ID = "wide-fresh-v1"
DEFAULT_WIDE_FRESH_BOOK = "launch_guard_wide_fresh_capital.json"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class WideFreshCapitalBook(CapitalBook):
    AGENTS: tuple[tuple[str, AgentRole], ...] = (
        (WIDE_FRESH_AGENT_ID, AgentRole.OPPORTUNITY_HUNTER),
    )


def fresh_setup_reason(
    gate: dict[str, dict[str, Any]], mint: str, decision: str,
    candidate: dict[str, Any],
) -> str | None:
    """None if this mint may be bought on this decision now; otherwise why
    not. Gated only by a mint's own last close on the *same* decision type -
    a different signal type, or a mint never closed before, is unrestricted.
    "Fresh" means a new high above the peak reached during that last
    position, which the entry review's own pullback check must then confirm
    (this only gates whether that check is allowed to run again)."""
    state = gate.get(mint)
    if state is None or state.get("decision") != decision:
        return None
    prior_peak = float(state.get("peak_price") or 0)
    if prior_peak <= 0:
        return None
    current_peak = float(candidate.get("peak_price") or candidate.get("price") or 0)
    if current_peak > prior_peak:
        return None
    return (
        f"same {decision} setup: no new high above {prior_peak:.6g} "
        f"since the last exit (peak since then {current_peak:.6g})"
    )


def _record_gate(
    book: CapitalBook, agent_id: str, mint: str, position: dict[str, Any],
    quote: dict[str, Any],
) -> None:
    """After closing a position, remember its decision and the peak to clear
    before it can be bought again.

    Prefers the board's own peak tracker (quote["peak_price"], the same
    longer-window high assess_entry measures pullbacks from) over this
    position's own highest_price_since_entry: a quick stop-out can close
    with a peak no higher than its own entry price, which would make any
    later candidate near that price read as a "new high" and defeat the
    gate - exactly the HYPE case this exists to catch. Falls back to the
    position's own high only when no board quote is available (off-board).
    """
    payload = book.load()
    if payload is None:
        raise ValueError("initialize agent capital first")
    account = payload["agents"][agent_id]
    gate = account.setdefault("fresh_gate", {})
    board_peak = float(quote.get("peak_price") or 0)
    peak = board_peak if board_peak > 0 else float(
        position.get("highest_price_since_entry") or 0)
    gate[mint] = {
        "decision": position.get("decision"),
        "peak_price": peak,
        "closed_at": datetime.now(UTC).isoformat(),
    }
    book._write(payload)  # noqa: SLF001 - same-package persistence helper


async def wide_fresh_cycle(
    book: CapitalBook, snapshot: dict[str, Any], *,
    policy: ShadowRecoveryPolicy,
    decisions: Sequence[str] = DEFAULT_WIDE_DECISIONS,
    max_open_positions: int = 4,
    order_usd: float = 5.0,
    max_daily_loss_usd: float = 3.0,
    round_trip_cost_pct: float = 1.2,
    fetch_quotes: QuoteFetcher = dexscreener_quotes,
    now: float | None = None,
    agent_id: str = WIDE_FRESH_AGENT_ID,
) -> dict[str, Any]:
    """Same as wide_shadow.wide_cycle, plus the fresh-setup re-entry gate on
    top of the shared re-entry rule. `agent_id` lets another paper book
    (momentum-take-v1) reuse it under its own name."""
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
            LOGGER.warning("wide-fresh: off-board quotes failed: %s", exc)
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
        if fraction >= 1.0:
            _record_gate(book, agent_id, mint, marked, quote)
        exits.append(review)

    entry: dict[str, Any] | None = None
    payload = book.load() or {}
    account = payload.get("agents", {}).get(agent_id, {})
    held = set(account.get("positions", {}))
    gate = account.get("fresh_gate", {})
    daily = book.daily_realized_pnl(agent_id)
    if (len(held) < max_open_positions
            and float(account.get("cash_usd", 0)) >= order_usd
            and daily > -max_daily_loss_usd):
        profile = StrategyProfile.from_env()
        ready: list[tuple[dict[str, Any], str | None]] = []
        for candidate in observed:
            mint = candidate["mint"]
            decision = str(candidate.get("decision"))
            if mint in held or decision not in decisions:
                continue
            blocked_fresh = fresh_setup_reason(gate, mint, decision, candidate)
            if blocked_fresh is not None:
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
                max_order_usd=order_usd,
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
        prog="launch-guard-wide-fresh",
        description="wide-fresh-v1: wide-v1 plus a fresh-setup re-entry gate "
        "(paper only, never trades real money)")
    parser.add_argument(
        "--book", default=os.getenv("WIDE_FRESH_BOOK_PATH", DEFAULT_WIDE_FRESH_BOOK))
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    book = WideFreshCapitalBook(args.book)
    book.initialize(30)
    if args.status:
        payload = book.load() or {}
        account = payload.get("agents", {}).get(WIDE_FRESH_AGENT_ID, {})
        print(json.dumps({**book.performance(WIDE_FRESH_AGENT_ID),
                          "open": account.get("positions", {}),
                          "fresh_gate": account.get("fresh_gate", {})}, indent=2))
        return
    policy = ShadowRecoveryPolicy.from_env()
    order_usd = paper_order_usd()
    decisions = wide_decisions()
    max_open = int(_env_float("WIDE_MAX_OPEN_POSITIONS", 4))
    snapshot_path = os.getenv("RECOMMENDATION_SNAPSHOT_PATH",
                              "launch_guard_recommendations.json")
    LOGGER.warning("wide-fresh-v1 (paper only): %s, %d open positions max",
                   ", ".join(decisions), max_open)
    while True:
        try:
            result = asyncio.run(wide_fresh_cycle(
                book, _read_snapshot(snapshot_path), policy=policy,
                decisions=decisions, max_open_positions=max_open,
                order_usd=order_usd,
                max_daily_loss_usd=paper_daily_loss_usd(order_usd)))
            for review in result["exits"]:
                if "fill" in review:
                    LOGGER.info("WIDE-FRESH SELL %s %s %s", review["mint"][:8],
                                review["state"], review["reasons"][-1])
            if result["entry"]:
                entry = result["entry"]
                LOGGER.info("WIDE-FRESH BUY %s %s @ %s%s", entry["symbol"],
                            entry["decision"], entry["price"],
                            " (live would skip)" if entry["live_blocked"] else "")
        except (ValueError, OSError, RuntimeError) as exc:
            LOGGER.warning("wide-fresh cycle skipped: %s", exc)
        if args.once:
            return
        time.sleep(max(args.interval, 30.0))


if __name__ == "__main__":
    main()
