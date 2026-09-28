"""Legacy swing paper book: exits only, no independent buys or additions.

New holds are delegated on existing positions by swing_manager inside portfolio
management. SwingSettings remains an offline historical replay preset.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .agent_capital import CapitalBook
from .agents import AgentRole
from .evaluation import LadderRules
from .hunter_shadow_strategy import (
    ShadowRecoveryPolicy,
    assess_exit,
)
from .live_trial_ledger import principal_sale_fraction

LOGGER = logging.getLogger("solana_launch_guard.swing")

SWING_AGENT_ID = "swing-v1"
DEFAULT_SWING_BOOK = "launch_guard_swing_capital.json"
# Far longer than any hold: assess_exit's stagnation exit never fires.
_NO_STAGNATION_SECONDS = 10.0**12


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class SwingSettings:
    stop_loss_pct: float = 35.0
    trailing_activation_pct: float = 50.0
    trailing_stop_pct: float = 25.0
    principal_recovered_trailing_stop_pct: float = 40.0
    principal_multiple: float = 2.0
    half_profit_multiple: float = 4.0
    second_stage_fraction: float = 0.5
    momentum_exit_pct: float = -15.0
    sell_pressure_ratio: float = 2.5
    max_hold_hours: float = 24.0
    order_usd: float = 5.0
    max_open_positions: int = 2
    max_daily_loss_usd: float = 3.0      # same as the live trial: 10% of $30
    add_trigger_pct: float = 20.0
    add_fraction: float = 0.5            # of the first stake: $2.50 on $5
    max_adds: int = 2
    # No add when pool liquidity has fallen below this share of the entry
    # liquidity: averaging into a drained pool is feeding a rug.
    add_min_liquidity_retention: float = 0.7
    round_trip_cost_pct: float = 1.2     # measured: ~60 bps per side

    @classmethod
    def from_env(cls) -> SwingSettings:
        d = cls()
        return cls(
            stop_loss_pct=_env_float("SWING_STOP_LOSS_PCT", d.stop_loss_pct),
            trailing_activation_pct=_env_float(
                "SWING_TRAILING_ACTIVATION_PCT", d.trailing_activation_pct),
            trailing_stop_pct=_env_float(
                "SWING_TRAILING_STOP_PCT", d.trailing_stop_pct),
            principal_recovered_trailing_stop_pct=_env_float(
                "SWING_PRINCIPAL_TRAILING_STOP_PCT",
                d.principal_recovered_trailing_stop_pct),
            principal_multiple=_env_float(
                "SWING_PRINCIPAL_MULTIPLE", d.principal_multiple),
            half_profit_multiple=_env_float(
                "SWING_HALF_PROFIT_MULTIPLE", d.half_profit_multiple),
            momentum_exit_pct=_env_float(
                "SWING_MOMENTUM_EXIT_PCT", d.momentum_exit_pct),
            sell_pressure_ratio=_env_float(
                "SWING_SELL_PRESSURE_RATIO", d.sell_pressure_ratio),
            max_hold_hours=_env_float("SWING_MAX_HOLD_HOURS", d.max_hold_hours),
            round_trip_cost_pct=_env_float(
                "SWING_ROUND_TRIP_COST_PCT", d.round_trip_cost_pct),
            add_trigger_pct=_env_float("SWING_ADD_TRIGGER_PCT", d.add_trigger_pct),
            add_fraction=_env_float("SWING_ADD_FRACTION", d.add_fraction),
            max_adds=int(_env_float("SWING_MAX_ADDS", d.max_adds)),
        )

    @property
    def max_hold_seconds(self) -> float:
        return self.max_hold_hours * 3600

    def ladder_rules(self, base: LadderRules | None = None) -> LadderRules:
        """The same exits for the evaluator (`--exit-model swing`)."""
        return replace(
            base or LadderRules(),
            stop_loss_pct=self.stop_loss_pct,
            principal_multiple=self.principal_multiple,
            half_profit_multiple=self.half_profit_multiple,
            second_stage_fraction=self.second_stage_fraction,
            trailing_activation_pct=self.trailing_activation_pct,
            trailing_stop_pct=self.trailing_stop_pct,
            principal_recovered_trailing_stop_pct=(
                self.principal_recovered_trailing_stop_pct),
            stagnation_enabled=False,
            max_hold_seconds=self.max_hold_seconds,
            lock_after_gain_pct=0.0,
            early_take_pct=0.0,
            add_trigger_pct=self.add_trigger_pct,
            add_fraction=self.add_fraction,
            max_adds=self.max_adds,
            add_min_liquidity_retention=self.add_min_liquidity_retention,
        )

    def exit_policy(self, base: ShadowRecoveryPolicy) -> ShadowRecoveryPolicy:
        """hunter-v1's exit policy with the swing settings in place."""
        return replace(
            base,
            stop_loss_pct=self.stop_loss_pct,
            trailing_activation_pct=self.trailing_activation_pct,
            trailing_stop_pct=self.trailing_stop_pct,
            principal_recovered_trailing_stop_pct=(
                self.principal_recovered_trailing_stop_pct),
            principal_multiple=self.principal_multiple,
            half_profit_multiple=self.half_profit_multiple,
            second_stage_fraction=self.second_stage_fraction,
            momentum_exit_pct=self.momentum_exit_pct,
            sell_pressure_ratio=self.sell_pressure_ratio,
            stagnation_window_seconds=_NO_STAGNATION_SECONDS,
            lock_after_gain_pct=0.0,
        )


class SwingCapitalBook(CapitalBook):
    """Its own paper book, so swing results never mix with hunter-v1's."""

    AGENTS: tuple[tuple[str, AgentRole], ...] = (
        (SWING_AGENT_ID, AgentRole.OPPORTUNITY_HUNTER),
    )


def review_swing_exit(
    position: dict[str, Any], quote: dict[str, Any], policy: ShadowRecoveryPolicy,
    settings: SwingSettings, *, now: float,
) -> dict[str, Any]:
    """assess_exit with the swing policy, plus the max hold and a price-only
    trailing stop when the quote has no buy/sell flow (DEX Screener batch)."""
    review = assess_exit(position, quote, policy, now=now)
    if review["state"] in {"EXIT", "EMERGENCY_EXIT"}:
        return review
    opened = _opened_epoch(position)
    if opened and now - opened >= settings.max_hold_seconds:
        return {**review, "state": "EXIT",
                "reasons": review["reasons"] + [
                    f"max hold {settings.max_hold_hours:g}h reached"]}
    flow_known = quote.get("buys_m5") is not None and quote.get("sells_m5") is not None
    if not flow_known:
        entry = float(position.get("entry_price") or 0)
        price = float(quote.get("price") or 0)
        peak = max(float(position.get("highest_price_since_entry") or 0), entry, price)
        if entry > 0 and price > 0:
            peak_gain = (peak / entry - 1) * 100
            drawdown = (peak - price) / peak * 100
            secured = bool(position.get("principal_secured",
                                        position.get("principal_recovered")))
            trail = (policy.principal_recovered_trailing_stop_pct if secured
                     else policy.trailing_stop_pct)
            if peak_gain >= policy.trailing_activation_pct and drawdown >= trail:
                return {**review, "state": "EXIT", "reasons": review["reasons"] + [
                    "trailing stop on price alone (no flow data off the board)"]}
    return review


def _opened_epoch(position: dict[str, Any]) -> float:
    try:
        return datetime.fromisoformat(str(position.get("opened_at"))).timestamp()
    except ValueError:
        return 0.0


QuoteFetcher = Callable[[Sequence[str]], Awaitable[dict[str, dict[str, Any]]]]


async def dexscreener_quotes(mints: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Price held mints the board no longer covers (USD, no flow data)."""
    from .outcome_tracker import BATCH_SIZE, DexScreenerBatchClient

    client = DexScreenerBatchClient()
    out: dict[str, dict[str, Any]] = {}
    mints = list(mints)
    for start in range(0, len(mints), BATCH_SIZE):
        quotes = await client.quotes(mints[start:start + BATCH_SIZE])
        for mint, quote in quotes.items():
            if quote.price_usd:
                out[mint] = {"price": quote.price_usd, "price_currency": "USD",
                             "liquidity_usd": quote.liquidity_usd}
    return out


def _fresh_board(snapshot: dict[str, Any], now: float) -> tuple[dict, list[dict]]:
    """(fresh quotes by mint, observed candidates) from the board snapshot,
    with the same 15-second freshness the hunter uses."""
    try:
        age = now - float(snapshot.get("generated_at") or 0)
    except (TypeError, ValueError):
        return {}, []
    if not 0 <= age <= 15:
        return {}, []
    rows = [r for r in (snapshot.get("tracked_candidates") or [])
            + (snapshot.get("candidates") or [])
            if isinstance(r, dict) and r.get("chain") == "solana"
            and isinstance(r.get("mint"), str)]
    fresh = {}
    for row in rows:
        try:
            if 0 <= now - float(row.get("quoted_at") or 0) <= 15:
                fresh[row["mint"]] = row
        except (TypeError, ValueError):
            continue
    observed = [r for r in (snapshot.get("candidates") or [])
                if isinstance(r, dict) and r.get("mint") in fresh]
    return fresh, observed


async def swing_cycle(
    book: SwingCapitalBook, snapshot: dict[str, Any], *,
    settings: SwingSettings, policy: ShadowRecoveryPolicy,
    fetch_quotes: QuoteFetcher = dexscreener_quotes,
    now: float | None = None,
) -> dict[str, Any]:
    """Exit-only compatibility loop for positions in the old swing book."""

    at = time.time() if now is None else now
    exit_policy = settings.exit_policy(policy)
    fresh, _ = _fresh_board(snapshot, at)
    payload = book.load() or {}
    account = payload.get("agents", {}).get(SWING_AGENT_ID, {})
    positions = dict(account.get("positions", {}))
    off_board = [m for m in positions if m not in fresh]
    fetched: dict[str, dict[str, Any]] = {}
    if off_board:
        try:
            fetched = await fetch_quotes(off_board)
        except Exception as exc:  # noqa: BLE001 - a quote outage must not stop the loop
            LOGGER.warning("swing: off-board quotes failed: %s", exc)
    exits: list[dict[str, Any]] = []
    for mint, position in positions.items():
        quote = fresh.get(mint) or fetched.get(mint)
        if quote is None:
            exits.append({"mint": mint, "state": "HOLD", "reasons": ["no quote"]})
            continue
        if str(quote.get("price_currency") or "USD") != str(
                position.get("price_currency") or "USD"):
            exits.append({"mint": mint, "state": "HOLD",
                          "reasons": ["quote currency differs from entry"]})
            continue
        price = float(quote.get("price") or 0)
        if not math.isfinite(price) or price <= 0:
            continue
        marked = book.mark_shadow_position(SWING_AGENT_ID, mint, price)
        review = review_swing_exit(marked, quote, exit_policy, settings, now=at)
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
                fraction, stage = settings.second_stage_fraction, "SECOND_STAGE"
        else:
            exits.append(review)
            continue
        review["fill"] = book.close_shadow_position(
            SWING_AGENT_ID, mint, fraction=fraction, stage=stage,
            slippage_pct=settings.round_trip_cost_pct,
        )
        exits.append(review)

    daily = book.daily_realized_pnl(SWING_AGENT_ID)
    return {"agent_id": SWING_AGENT_ID, "mode": "shadow", "live_execution": False,
            "exits": exits, "entry": None, "daily_realized_pnl_usd": daily,
            "at": datetime.fromtimestamp(at, UTC).isoformat()}


def _read_snapshot(path: str) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def main(argv: Sequence[str] | None = None) -> None:
    from .eval_cli import _load_dotenv

    _load_dotenv()
    parser = argparse.ArgumentParser(
        prog="launch-guard-swing",
        description="legacy swing paper book: exits only; new holds use portfolio delegation",
    )
    parser.add_argument(
        "--book", default=os.getenv("SWING_BOOK_PATH", DEFAULT_SWING_BOOK))
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    book = SwingCapitalBook(args.book)
    book.initialize(30)
    if args.status:
        payload = book.load() or {}
        open_positions = payload.get("agents", {}).get(SWING_AGENT_ID, {}).get(
            "positions", {})
        print(json.dumps({**book.performance(SWING_AGENT_ID), "open": open_positions},
                         indent=2))
        return
    settings = SwingSettings.from_env()
    policy = ShadowRecoveryPolicy.from_env()
    snapshot_path = os.getenv("RECOMMENDATION_SNAPSHOT_PATH",
                              "launch_guard_recommendations.json")
    LOGGER.warning("swing-v1 (paper only) %s", settings)
    while True:
        try:
            result = asyncio.run(swing_cycle(
                book, _read_snapshot(snapshot_path), settings=settings, policy=policy))
            for review in result["exits"]:
                if "fill" in review:
                    LOGGER.info("SWING SELL %s %s %s", review["mint"][:8],
                                review["state"], review["reasons"][-1])
            if result["entry"]:
                LOGGER.info("SWING BUY %s %s @ %s", result["entry"]["symbol"],
                            result["entry"]["decision"], result["entry"]["price"])
        except (ValueError, OSError, RuntimeError) as exc:
            LOGGER.warning("swing cycle skipped: %s", exc)
        if args.once:
            return
        time.sleep(max(args.interval, 30.0))


if __name__ == "__main__":
    main()
