"""Bounded Solana canary watch: at most one $5 buy, then monitor its exit."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .agent_live_test import CanaryJournal, LIVE_CONFIRMATION, run_live_canary, validate_live_environment


async def watch(*, hours: float = 8, interval: float = 15, clock=None) -> dict:
    if not 0 < hours <= 8 or interval < 15:
        raise ValueError("trial must last at most 8 hours and poll no faster than 15 seconds")
    validate_live_environment(execute=True, confirmation=LIVE_CONFIRMATION)
    if os.getenv("AUTO_BUY_ENABLED", "false").lower() in {"true", "1", "yes", "on"} and os.getenv("AUTO_BUY_LIVE", "false").lower() in {"true", "1", "yes", "on"}:
        raise ValueError("turn AUTO_BUY_LIVE off: the overnight trial permits only one canary purchase")
    journal = CanaryJournal(os.getenv("AGENT_LIVE_CANARY_PATH", "launch_guard_live_canary.json"))
    journal.assert_unused()
    # One monitor owns both recommendation and portfolio snapshots. The canary
    # is the only buyer; canary-only mode prevents selling other wallet assets.
    env = os.environ.copy()
    env.update(AGENT_LIVE_CANARY_ONLY="true", AUTO_BUY_LIVE="false", AUTO_REBUY_ENABLED="false")
    monitor = subprocess.Popen(
        [sys.executable, "-m", "solana_launch_guard.app", "--mode", "launches", "--portfolio-window"],
        env=env,
    )
    now = clock or time.monotonic
    deadline = now() + hours * 3600
    result: dict = {"status": "NO_ELIGIBLE_SIGNAL", "broadcast": False}
    snapshot = Path(os.getenv("RECOMMENDATION_SNAPSHOT_PATH", "launch_guard_recommendations.json"))
    try:
        while now() < deadline:
            if monitor.poll() is not None:
                raise RuntimeError(f"Launch Guard monitor stopped (exit {monitor.returncode}); trial stopped")
            # Environment variables may be changed only in the shell of a new
            # process. Ctrl+C terminates the monitor; journals prevent retries.
            validate_live_environment(execute=True, confirmation=LIVE_CONFIRMATION)
            if journal.path.with_suffix(journal.path.suffix + ".claim").exists() or journal.load()["attempts"]:
                raise RuntimeError("Canary already attempted; inspect journal and wallet before restarting")
            if not snapshot.is_file():
                await asyncio.sleep(interval)
                continue
            try:
                result = await run_live_canary(execute=True, confirmation=LIVE_CONFIRMATION)
                # Keep monitoring the acquired mint for protective exits until
                # the deadline. This process cannot make a second purchase.
                break
            except ValueError as exc:
                if str(exc) not in {
                    "recommendation snapshot is missing or stale",
                    "recommendation snapshot has no candidates",
                    "no fresh, confirmed, sufficiently liquid Solana buy candidate",
                }:
                    raise
            await asyncio.sleep(interval)
        while result.get("broadcast") and now() < deadline:
            if monitor.poll() is not None:
                raise RuntimeError(f"Launch Guard monitor stopped (exit {monitor.returncode}); check wallet")
            await asyncio.sleep(interval)
        return result
    finally:
        if monitor.poll() is None:
            monitor.terminate()
            try:
                monitor.wait(timeout=10)
            except subprocess.TimeoutExpired:
                monitor.kill()
                monitor.wait()


def main() -> None:
    from .config import _load_dotenv

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=8)
    args = parser.parse_args()
    _load_dotenv()
    # Deliberately do not enable execution flags, alter the user's .env, or
    # clear a previous claim. The pre-existing canary checks every spend.
    try:
        print(json.dumps(asyncio.run(watch(hours=args.hours)), indent=2))
    except (ValueError, RuntimeError, KeyboardInterrupt) as exc:
        raise SystemExit(f"Trial stopped: {exc}") from None


if __name__ == "__main__":
    main()
