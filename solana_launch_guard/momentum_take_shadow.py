"""momentum-take-v1: a paper-only book testing exits built for win rate.

Replays of the 2026-09-25..10-01 signals showed the win rate is set mostly
by exits: most signals reach break-even and stall, and the 5-minute
stagnation exit then closes them at a small loss. For MOMENTUM BUY, selling
everything at +4% and giving a trade 30 minutes before the stagnation exit
lifted the replayed win rate from 17% to 62% and cut the loss per trade from
$0.205 to $0.017. That was measured on the data the settings were chosen
from, so this book checks it on fresh signals.

It is wide-fresh-v1's cycle (wide_fresh_shadow.wide_fresh_cycle: same entry
review, shared re-entry rule plus the fresh-setup gate - no re-buy on the
same signal until a new high above the last exit - PAPER_ORDER_USD per
position, 1.2% round-trip cost, $3 daily loss limit) under its own agent and
capital book, with two exit changes applied only here:

  - MOMENTUM_TAKE_PCT (default 4): sell the whole position at this gain
    (ShadowRecoveryPolicy.early_take_pct).
  - MOMENTUM_TAKE_STAGNATION_SECONDS (default 1800): stagnation window.
  - MOMENTUM_TAKE_STOP_LOSS_PCT (default: the shared STOP_LOSS_PCT): hard stop.
  - MOMENTUM_TAKE_MAX_HOLD_SECONDS (default 0 = off): sell whatever is left after
    this long, regardless of momentum.
  - MOMENTUM_TAKE_YOUNG_MINUTES (default 0 = off) and MOMENTUM_TAKE_YOUNG_*: tokens
    younger than this aim for a 10-15% take instead of +2%, keeping that target only
    if they pulled back and recovered within the first minute, else falling back to
    the +2% take (see ShadowRecoveryPolicy.young_*).
  - MOMENTUM_TAKE_INTERVAL_SECONDS (default 15, the minimum): poll cadence.

MOMENTUM BUY is paused for live trading; this book trades it on paper only
and records whether the live gate would have refused each entry. It never
touches real money and never calls the model. Compare it with the other
books in `launch-guard-eval books`.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import math
import os
import time
from collections.abc import Sequence

from .agent_capital import CapitalBook
from .agents import AgentRole
from .hunter_shadow_strategy import (
    ShadowRecoveryPolicy,
    paper_min_token_age_minutes,
    paper_rug_block_hours,
    paper_rug_loss_pct,
    paper_young_buy_minutes,
    paper_young_rebuy_after_minutes,
)
from .swing_strategy import _read_snapshot
from .wide_fresh_shadow import wide_fresh_cycle
from .wide_shadow import paper_daily_loss_usd, paper_order_usd

LOGGER = logging.getLogger("solana_launch_guard.momentum_take")

MOMENTUM_TAKE_AGENT_ID = "momentum-take-v1"
DEFAULT_MOMENTUM_TAKE_BOOK = "launch_guard_momentum_take_capital.json"
DEFAULT_MOMENTUM_TAKE_DECISIONS = ("MOMENTUM BUY",)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class MomentumTakeCapitalBook(CapitalBook):
    AGENTS: tuple[tuple[str, AgentRole], ...] = (
        (MOMENTUM_TAKE_AGENT_ID, AgentRole.OPPORTUNITY_HUNTER),
    )


def momentum_take_min_token_age_minutes() -> float:
    """MOMENTUM_TAKE_MIN_TOKEN_AGE_MINUTES overrides the shared
    PAPER_MIN_TOKEN_AGE_MINUTES for this book only. This is the short-hold,
    take-profit book, so it is the designated place to keep testing very
    young tokens while hunter and the wide books skip them. Unset follows the
    shared guard."""
    raw = os.getenv("MOMENTUM_TAKE_MIN_TOKEN_AGE_MINUTES")
    if raw is None or not raw.strip():
        return paper_min_token_age_minutes()
    amount = float(raw)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError(
            "MOMENTUM_TAKE_MIN_TOKEN_AGE_MINUTES must be zero or positive")
    return amount


def momentum_take_decisions() -> tuple[str, ...]:
    raw = os.getenv("MOMENTUM_TAKE_DECISIONS", "")
    chosen = tuple(d.strip().upper() for d in raw.split(",") if d.strip())
    return chosen or DEFAULT_MOMENTUM_TAKE_DECISIONS


def momentum_take_policy(
    base: ShadowRecoveryPolicy | None = None,
) -> ShadowRecoveryPolicy:
    """The shared policy with this book's two exit settings. Live and the
    other books keep reading EARLY_TAKE_PCT / STAGNATION_WINDOW_SECONDS."""
    base = base or ShadowRecoveryPolicy.from_env()
    take = _env_float("MOMENTUM_TAKE_PCT", 4.0)
    window = _env_float("MOMENTUM_TAKE_STAGNATION_SECONDS", 1800.0)
    stop = _env_float("MOMENTUM_TAKE_STOP_LOSS_PCT", base.stop_loss_pct)
    max_hold = _env_float("MOMENTUM_TAKE_MAX_HOLD_SECONDS", 0.0)
    if take <= 0 or window <= 0 or not 0 < stop < 100 or max_hold < 0:
        raise ValueError(
            "MOMENTUM_TAKE_PCT and MOMENTUM_TAKE_STAGNATION_SECONDS must be "
            "positive, MOMENTUM_TAKE_STOP_LOSS_PCT between 0 and 100 and "
            "MOMENTUM_TAKE_MAX_HOLD_SECONDS zero or positive"
        )
    young = _env_float("MOMENTUM_TAKE_YOUNG_MINUTES", 0.0)
    if young < 0:
        raise ValueError("MOMENTUM_TAKE_YOUNG_MINUTES must be zero or positive")
    return dataclasses.replace(
        base, early_take_pct=take, stagnation_window_seconds=window,
        stop_loss_pct=stop, max_hold_seconds=max_hold,
        young_token_minutes=young,
        young_take_pct=_env_float("MOMENTUM_TAKE_YOUNG_TAKE_PCT", 10.0),
        young_take_max_pct=_env_float("MOMENTUM_TAKE_YOUNG_TAKE_MAX_PCT", 15.0),
        young_trail_pct=_env_float("MOMENTUM_TAKE_YOUNG_TRAIL_PCT", 2.0),
        young_window_seconds=_env_float("MOMENTUM_TAKE_YOUNG_WINDOW_SECONDS", 60.0),
        young_pullback_pct=_env_float("MOMENTUM_TAKE_YOUNG_PULLBACK_PCT", 3.0),
        young_rise_pct=_env_float("MOMENTUM_TAKE_YOUNG_RISE_PCT", 3.0),
        young_max_hold_seconds=_env_float(
            "MOMENTUM_TAKE_YOUNG_MAX_HOLD_SECONDS", 600.0),
    )


def main(argv: Sequence[str] | None = None) -> None:
    from .eval_cli import _load_dotenv

    _load_dotenv()
    parser = argparse.ArgumentParser(
        prog="launch-guard-momentum-take",
        description="momentum-take-v1: paper-only book for MOMENTUM BUY with a "
        "+4%% full take-profit and a 30-minute stagnation exit (never trades "
        "real money)",
    )
    parser.add_argument(
        "--book",
        default=os.getenv("MOMENTUM_TAKE_BOOK_PATH", DEFAULT_MOMENTUM_TAKE_BOOK),
    )
    parser.add_argument(
        "--interval", type=float,
        default=_env_float("MOMENTUM_TAKE_INTERVAL_SECONDS", 15.0))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    book = MomentumTakeCapitalBook(args.book)
    book.initialize(40)  # four $10 positions
    if args.status:
        payload = book.load() or {}
        open_positions = (
            payload.get("agents", {})
            .get(MOMENTUM_TAKE_AGENT_ID, {})
            .get("positions", {})
        )
        print(
            json.dumps(
                {**book.performance(MOMENTUM_TAKE_AGENT_ID), "open": open_positions},
                indent=2,
            )
        )
        return
    policy = momentum_take_policy()
    order_usd = paper_order_usd()
    decisions = momentum_take_decisions()
    max_open = int(_env_float("MOMENTUM_TAKE_MAX_OPEN_POSITIONS", 4))
    snapshot_path = os.getenv(
        "RECOMMENDATION_SNAPSHOT_PATH", "launch_guard_recommendations.json"
    )
    LOGGER.warning(
        "momentum-take-v1 (paper only): %s, take +%g%%, stagnation %gs, "
        "%d open positions max",
        ", ".join(decisions),
        policy.early_take_pct,
        policy.stagnation_window_seconds,
        max_open,
    )
    while True:
        try:
            result = asyncio.run(
                wide_fresh_cycle(
                    book,
                    _read_snapshot(snapshot_path),
                    policy=policy,
                    decisions=decisions,
                    max_open_positions=max_open,
                    agent_id=MOMENTUM_TAKE_AGENT_ID,
                    order_usd=order_usd,
                    max_daily_loss_usd=paper_daily_loss_usd(order_usd),
                    min_token_age_minutes=momentum_take_min_token_age_minutes(),
                    rug_block_hours=paper_rug_block_hours(),
                    rug_loss_pct=paper_rug_loss_pct(),
                    young_buy_minutes=paper_young_buy_minutes(),
                    young_rebuy_after_minutes=paper_young_rebuy_after_minutes(),
                )
            )
            for review in result["exits"]:
                if "fill" in review:
                    LOGGER.info(
                        "MOMENTUM-TAKE SELL %s %s %s",
                        review["mint"][:8],
                        review["state"],
                        review["reasons"][-1],
                    )
            if result["entry"]:
                entry = result["entry"]
                LOGGER.info(
                    "MOMENTUM-TAKE BUY %s %s @ %s%s",
                    entry["symbol"],
                    entry["decision"],
                    entry["price"],
                    " (live would skip)" if entry["live_blocked"] else "",
                )
        except (ValueError, OSError, RuntimeError) as exc:
            LOGGER.warning("momentum-take cycle skipped: %s", exc)
        if args.once:
            return
        time.sleep(max(args.interval, 15.0))


if __name__ == "__main__":
    main()
