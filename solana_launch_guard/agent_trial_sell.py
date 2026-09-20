"""Strictly limited extra wallet exit during a one-canary overnight trial."""
from __future__ import annotations

import math
import os

from .agent_live_test import CanaryJournal


def extra_exit_eligible(signal, balance, *, minimum_usd: float) -> bool:
    enabled = lambda name: os.getenv(name, "false").strip().lower() in {"true", "1", "yes", "on"}
    if not (enabled("AGENT_LIVE_CANARY_ONLY") and enabled("AGENT_LIVE_EXTRA_SMALL_SELL")
            and enabled("AGENT_LIVE_TEST_ENABLED") and not enabled("AGENT_LIVE_KILL_SWITCH")):
        return False
    if signal.decision != "EXIT WARNING" or balance.raw_amount <= 0:
        return False
    try:
        amount = float(signal.current_value_usd)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(amount) or not max(2, minimum_usd) <= amount <= 5:
        return False
    journal = CanaryJournal(os.getenv("AGENT_LIVE_EXTRA_SELL_PATH", "launch_guard_live_extra_sell.json"))
    try:
        journal.assert_unused()
    except ValueError:
        return False
    return True
