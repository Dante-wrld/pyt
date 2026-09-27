"""Shadow-only delegation of existing positions; never opens or adds a trade."""

from __future__ import annotations

import json
import math
import os
from dataclasses import replace
from pathlib import Path

from .hunter_shadow_strategy import ShadowRecoveryPolicy, assess_exit
from .trend_pullback import fresh_evidence

MAX_HOLD_SECONDS = 24 * 3600


def load_evidence() -> dict:
    try:
        payload = json.loads(
            Path(
                os.getenv(
                    "SHADOW_TREND_EVIDENCE_PATH", "launch_guard_trend_evidence.json"
                )
            ).read_text()
        )
        return payload.get("tokens", {}) if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def managed_exit_review(
    position: dict,
    quote: dict,
    policy: ShadowRecoveryPolicy,
    evidence: dict,
    *,
    now: float,
) -> dict:
    """Only extend a stagnation exit, with current evidence of potential.

    Ownership, original stop, profit stages and budget remain with the entry
    agent. Strong trend is a testable proxy for potential, not a prediction.
    Even after delegation, every ordinary risk exit keeps its priority.
    """
    from .swing_strategy import _opened_epoch

    normal = assess_exit(position, quote, policy, now=now)
    was_managed = position.get("exit_manager") == "swing-v1"
    opened = _opened_epoch(position)
    if was_managed and opened > 0 and now - opened >= MAX_HOLD_SECONDS:
        return {
            **normal,
            "state": "EXIT",
            "reasons": normal["reasons"]
            + ["swing manager: 24h maximum from original entry"],
        }
    stagnant = normal["state"] == "EXIT" and any(
        str(r).startswith("no sign of a rise") for r in normal["reasons"]
    )
    if (
        normal["state"] in {"EXIT", "EMERGENCY_EXIT", "REVERSAL_WARNING"}
        and not stagnant
    ):
        return normal  # risk exits and warnings cannot be delegated
    if (
        was_managed
        and fresh_evidence(evidence, now=now)
        and not evidence.get("trend_confirmed")
    ):
        return {
            **normal,
            "state": "EXIT",
            "reasons": normal["reasons"]
            + ["swing manager: closed-candle trend invalidated"],
        }
    if opened <= 0 or now - opened >= MAX_HOLD_SECONDS:
        return normal
    if not fresh_evidence(evidence, now=now) or not evidence.get("trend_confirmed"):
        return normal
    try:
        price = float(quote["price"])
        entry = float(position["entry_price"])
        liquidity = float(quote["liquidity_usd"])
        baseline = float(position["entry_liquidity_usd"])
        support = float(evidence["ema50"])
        buys, sells = float(quote["buys_m5"]), float(quote["sells_m5"])
    except (KeyError, TypeError, ValueError):
        return normal
    if (
        not all(
            math.isfinite(v)
            for v in (price, entry, liquidity, baseline, support, buys, sells)
        )
        or entry <= 0
        or baseline <= 0
        or support <= 0
        or price < support
        or price < entry * 0.97
        or liquidity < max(policy.min_liquidity_usd, baseline * 0.9)
        or buys <= 0
        or buys < sells
        or quote.get("price_currency") != "USD"
        or position.get("price_currency") != "USD"
    ):
        return normal
    extended = assess_exit(
        position,
        quote,
        replace(policy, stagnation_window_seconds=MAX_HOLD_SECONDS),
        now=now,
    )
    # The only relaxed rule is stagnation. Never widen the original hard stop.
    if extended["state"] in {"EXIT", "EMERGENCY_EXIT"}:
        return extended
    return {
        **extended,
        "swing_handoff": True,
        "exit_manager": "swing-v1",
        "evidence_as_of": evidence["as_of"],
        "reasons": extended["reasons"]
        + [
            "swing manager: fresh EMA uptrend, price within 3% of entry, "
            "liquidity retained and buying flow; defer stagnation only"
        ],
    }
