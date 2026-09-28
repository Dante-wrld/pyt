"""Paired paper research: baseline, trend-filtered entries, swing-managed exits.

No signer or live execution is instantiated. All three arms share entry
opportunities; this is a controlled experiment, not a full live-bot replica.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
from pathlib import Path

from .agent_capital import CapitalBook
from .agents import AgentRole
from .hunter_shadow_strategy import (
    ShadowRecoveryPolicy,
    assess_entry,
    assess_exit,
    reentry_block_reason,
)
from .live_trial_ledger import principal_sale_fraction
from .live_trial_runner import live_entry_gate
from .market import DexScreenerOracle
from .market_structure import MarketStructureScanner
from .swing_manager import managed_exit_review
from .swing_strategy import _fresh_board, _read_snapshot
from .trend_pullback import MIN_AGE_SECONDS, assess_trend_pullback, entry_confirmed

AGENT = "hunter-v1"
ARMS = ("baseline", "trend", "managed")


def _account(book: CapitalBook) -> dict:
    """This arm's account; a missing book is an error, as in agent_capital."""
    payload = book.load()
    if payload is None:
        raise ValueError("initialize agent capital first")
    return payload["agents"][AGENT]


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2))
    os.replace(tmp, path)


class ResearchCapitalBook(CapitalBook):
    AGENTS = ((AGENT, AgentRole.OPPORTUNITY_HUNTER),)


class ShadowComparison:
    """One process owns these books; no capital is shared with the real bot."""

    def __init__(self, directory: Path, *, cost_pct: float = 1.2,
                 max_open_positions: int | None = None):
        if not math.isfinite(cost_pct) or not 0 <= cost_pct < 100:
            raise ValueError("round-trip cost estimate must be in [0, 100)")
        # More slots = more paired trades per day; same $5 size and $3 daily
        # limit per arm. Paper only (TREND_SHADOW_MAX_OPEN_POSITIONS).
        self.max_open_positions = max_open_positions or int(
            os.getenv("TREND_SHADOW_MAX_OPEN_POSITIONS") or 4)
        self.directory, self.cost_pct = directory, cost_pct
        self.books = {
            arm: ResearchCapitalBook(directory / f"{arm}.json") for arm in ARMS
        }
        for book in self.books.values():
            book.initialize(30)
        # Do not mix fills made with different cost assumptions in one report.
        config = {"schema": 1, "round_trip_cost_pct": cost_pct}
        config_path = directory / "config.json"
        if config_path.exists() and json.loads(config_path.read_text()) != config:
            raise ValueError("experiment settings changed; use a new directory")
        write_json(config_path, config)

    def positions(self) -> dict:
        result = {}
        for book in self.books.values():
            result.update(_account(book)["positions"])
        return result

    def cycle(
        self,
        snapshot: dict,
        quotes: dict,
        evidence: dict,
        *,
        now: float,
        policy: ShadowRecoveryPolicy | None = None,
    ) -> dict:
        policy = policy or ShadowRecoveryPolicy.from_env()
        events = []
        held_at_start = set(self.positions())
        for arm, book in self.books.items():
            positions = dict(_account(book)["positions"])
            for mint, position in positions.items():
                quote = quotes.get(mint, {})
                try:
                    price = float(quote["price"])
                    fresh = 0 <= now - float(quote["quoted_at"]) <= 15
                except (KeyError, TypeError, ValueError):
                    continue
                if (
                    not fresh
                    or not math.isfinite(price)
                    or price <= 0
                    or quote.get("price_currency") != position["price_currency"]
                ):
                    continue
                marked = book.mark_shadow_position(AGENT, mint, price)
                review = (
                    managed_exit_review(
                        marked, quote, policy, evidence.get(mint, {}), now=now
                    )
                    if arm == "managed"
                    else assess_exit(marked, quote, policy, now=now)
                )
                if review.get("swing_handoff") and not book.delegate_shadow_exit(
                    AGENT, mint, review
                ):
                    review = assess_exit(marked, quote, policy, now=now)
                    review["reasons"].append("swing capacity unavailable")
                event = {"arm": arm, "mint": mint, **review}
                if review["state"] in {"EXIT", "EMERGENCY_EXIT", "TAKE_PARTIAL"}:
                    fraction, stage = 1.0, "EXIT"
                    if review["state"] == "TAKE_PARTIAL":
                        if not marked["principal_recovered"]:
                            fraction = principal_sale_fraction(
                                marked["allocated_usd"], marked["current_value_usd"]
                            )
                            stage = "PRINCIPAL_RECOVERY"
                        else:
                            fraction, stage = (
                                policy.second_stage_fraction,
                                "SECOND_STAGE",
                            )
                    # Charge the same explicit cost estimate in every arm.
                    event["fill"] = book.close_shadow_position(
                        AGENT,
                        mint,
                        fraction=fraction,
                        stage=stage,
                        slippage_pct=self.cost_pct,
                    )
                events.append(event)
        _, candidates = _fresh_board(snapshot, now)
        held = self.positions()
        ready = []
        for row in candidates:
            mint = row["mint"]
            if (
                mint in held
                or mint in held_at_start
                or row.get("price_currency") != "USD"
            ):
                continue
            try:
                old = now - float(row.get("pair_created_at_ms") or now * 1000) / 1000
                price = float(row["price"])
                impact = float(row.get("quoted_price_impact_pct") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if old < MIN_AGE_SECONDS or not math.isfinite(price) or price <= 0:
                continue
            if not math.isfinite(impact) or abs(impact) > 5:
                continue
            review = live_entry_gate(row, assess_entry(row, policy), now=now)
            if review["state"] != "BUY_READY":
                continue
            if any(
                reentry_block_reason(
                    book.sell_history(AGENT, mint, price_currency="USD"), price
                )
                for book in self.books.values()
            ):
                continue
            ready.append(row)
        # Paired entry cohort: both exit policies start the same trade at the
        # same price. Wait until all arms have room; never duplicate an open mint.
        can_enter = all(
            len(_account(book)["positions"]) < self.max_open_positions
            and _account(book)["cash_usd"] >= 5
            and book.daily_realized_pnl(AGENT) > -3
            for book in self.books.values()
        )
        entry = None
        if can_enter and ready:
            pick = max(ready, key=lambda r: float(r.get("signal_score") or 0))
            mint = pick["mint"]
            reading = evidence.get(mint, {})
            qualifies = entry_confirmed(reading, float(pick["price"]), now=now)
            entered = []
            for arm, book in self.books.items():
                if arm == "trend" and not qualifies:
                    continue
                book.reserve_shadow_buy(
                    agent_id=AGENT,
                    mint=mint,
                    symbol=str(pick.get("symbol") or mint[:6]),
                    amount_usd=5,
                    entry_price=float(pick["price"]),
                    price_currency="USD",
                    entry_liquidity_usd=float(pick["liquidity_usd"]),
                    pair_address=pick.get("pair_address"),
                    pair_created_at_ms=pick.get("pair_created_at_ms"),
                    max_open_positions=self.max_open_positions,
                    decision=str(pick.get("decision") or ""),
                )
                entered.append(arm)
            entry = {
                "mint": mint,
                "arms": entered,
                "evidence": reading,
                "entry_price": pick["price"],
                "at": now,
            }
        output = {
            "at": now,
            "mode": "shadow",
            "live_execution": False,
            "entry": entry,
            "reviews": events,
            "report": self.report(),
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "decisions.jsonl").open("a") as handle:
            handle.write(json.dumps(output) + "\n")
        write_json(self.directory / "report.json", self.report())
        return output

    def report(self) -> dict:
        return {
            "round_trip_cost_pct": self.cost_pct,
            "note": (
                "Matched entry cohort; fixed estimated costs; "
                "not live fills or proof of an edge."
            ),
            "arms": {
                arm: {
                    **book.performance(AGENT),
                    "swing_allocation": book.public_status()["swing_allocation"],
                }
                for arm, book in self.books.items()
            },
        }


async def collect(
    snapshot: dict,
    held: dict,
    scanner: MarketStructureScanner,
    oracle: DexScreenerOracle,
) -> tuple[dict, dict]:
    quotes, candidates = _fresh_board(snapshot, time.time())
    for mint in held:
        if mint in quotes:
            continue
        try:
            q = await oracle.quote(mint)
        except (OSError, ValueError, RuntimeError, TimeoutError):
            continue
        if q and q.price_usd:
            quotes[mint] = {
                "price": q.price_usd,
                "price_currency": "USD",
                "quoted_at": time.time(),
                "liquidity_usd": q.liquidity_usd,
                "pair_address": q.pair_address,
                "pair_created_at_ms": q.pair_created_at_ms,
                "buys_m5": q.buys_m5,
                "sells_m5": q.sells_m5,
                "price_change_m5_pct": q.price_change_m5_pct,
            }
    evidence = {}
    # Held positions have priority, then highest-ranked potential entries.
    ordered = list(held) + [
        c["mint"]
        for c in sorted(
            candidates, key=lambda c: float(c.get("signal_score") or 0), reverse=True
        )
        if c["mint"] not in held
    ]
    for mint in dict.fromkeys(ordered):
        row = quotes.get(mint, {})
        pool = str(
            row.get("pair_address") or held.get(mint, {}).get("pair_address") or ""
        )
        if not pool:
            continue
        bars = await scanner.trend_candles(pool=pool, mint=mint)
        try:
            age = float(
                row.get("pair_created_at_ms")
                or held.get(mint, {}).get("pair_created_at_ms")
                or 0
            )
        except (TypeError, ValueError):
            age = None
        reading = assess_trend_pullback(
            bars, now=time.time(), pair_created_at_ms=age or None
        )
        evidence[mint] = {**reading, "pool": pool, "mint": mint}
    return quotes, evidence


def main(argv: list[str] | None = None) -> None:
    import fcntl

    from .config import _load_dotenv

    _load_dotenv()
    parser = argparse.ArgumentParser(
        description="Paper-only trend/pullback and swing-manager comparison"
    )
    parser.add_argument("--directory", default="launch_guard_trend_shadow")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--round-trip-cost-pct", type=float, default=1.2)
    args = parser.parse_args(argv)
    directory = Path(args.directory)
    directory.mkdir(parents=True, exist_ok=True)
    # Prevent two research processes from modifying the same experiment books.
    with (directory / "runner.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        experiment = ShadowComparison(directory, cost_pct=args.round_trip_cost_pct)
        if args.status:
            print(json.dumps(experiment.report(), indent=2))
            return
        scanner, oracle = MarketStructureScanner(), DexScreenerOracle()
        while True:
            snapshot_path = os.getenv(
                "RECOMMENDATION_SNAPSHOT_PATH", "launch_guard_recommendations.json"
            )
            held = experiment.positions()
            # Also produce read-only evidence for the existing hunter shadow loop.
            source = _read_snapshot(
                os.getenv("AGENT_CAPITAL_PATH", "launch_guard_agent_capital.json")
            )
            held.update(source.get("agents", {}).get(AGENT, {}).get("positions", {}))
            quotes, evidence = asyncio.run(
                collect(_read_snapshot(snapshot_path), held, scanner, oracle)
            )
            write_json(
                Path(
                    os.getenv(
                        "SHADOW_TREND_EVIDENCE_PATH", "launch_guard_trend_evidence.json"
                    )
                ),
                {"generated_at": time.time(), "tokens": evidence},
            )
            # Candle I/O may have taken time: reload the board for entry freshness.
            snapshot = _read_snapshot(snapshot_path)
            latest, _ = _fresh_board(snapshot, time.time())
            quotes.update(latest)
            result = experiment.cycle(snapshot, quotes, evidence, now=time.time())
            print(json.dumps(result), flush=True)
            if args.once:
                return
            time.sleep(max(30, args.interval))


if __name__ == "__main__":
    main()
