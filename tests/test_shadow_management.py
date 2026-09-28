"""Shadow strategy regressions: no live execution, no invented capital."""

import time
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from solana_launch_guard.agent_capital import CapitalBook
from solana_launch_guard.hunter_shadow_strategy import ShadowRecoveryPolicy
from solana_launch_guard.market_structure import Candle
from solana_launch_guard.swing_manager import managed_exit_review
from solana_launch_guard.trend_pullback import PERIOD, assess_trend_pullback
from solana_launch_guard.trend_shadow import ShadowComparison

from test_hunter_shadow_strategy import candidate


def bars(now):
    end = int(now // PERIOD) * PERIOD
    prices = [1 + i * 0.001 for i in range(150)]
    result = [
        Candle(end - (150 - i) * PERIOD, p - 0.0005, p + 0.001, p - 0.001, p, 100)
        for i, p in enumerate(prices)
    ]
    # Actual pullback into EMA20, then a close back above the preceding high.
    result[-2] = replace(result[-2], open=1.147, low=1.136, close=1.138, high=1.148)
    result[-1] = replace(result[-1], open=1.140, low=1.139, close=1.150, high=1.151)
    return result


def evidence(now):
    return {
        "valid": True,
        "trend_confirmed": True,
        "pullback_confirmed": True,
        "as_of": now,
        "ema20": 0.99,
        "ema50": 0.95,
    }


def position(now):
    return {
        "entry_price": 1,
        "highest_price_since_entry": 1.02,
        "entry_liquidity_usd": 80000,
        "price_currency": "USD",
        "opened_at": datetime.fromtimestamp(now - 600, UTC).isoformat(),
        "principal_recovered": False,
        "principal_secured": False,
    }


def quote(now, price=1):
    return {
        "price": price,
        "price_currency": "USD",
        "liquidity_usd": 80000,
        "quoted_at": now,
        "buys_m5": 20,
        "sells_m5": 10,
        "price_change_m5_pct": -1,
    }


def test_closed_candle_pullback_and_history_guards():
    now = time.time()
    kwargs = dict(now=now, pair_created_at_ms=(now - 4 * 86400) * 1000)
    reading = assess_trend_pullback(bars(now), **kwargs)
    assert reading["trend_confirmed"] and reading["pullback_confirmed"]
    assert not assess_trend_pullback(bars(now)[:40], **kwargs)["valid"]
    assert not assess_trend_pullback(
        bars(now), now=now, pair_created_at_ms=(now - 86400) * 1000
    )["valid"]
    assert not assess_trend_pullback(
        bars(now),
        now=now + 2 * PERIOD,
        **{"pair_created_at_ms": kwargs["pair_created_at_ms"]},
    )["valid"]
    malformed = bars(now)
    malformed[-1] = replace(malformed[-1], close=float("nan"))
    assert not assess_trend_pullback(malformed, **kwargs)["valid"]
    gapped = bars(now)
    gapped[-2] = replace(gapped[-2], start=gapped[-2].start - 1)
    assert not assess_trend_pullback(gapped, **kwargs)["valid"]
    future = bars(now)
    future[-1] = replace(future[-1], start=int(now))
    assert not assess_trend_pullback(future, **kwargs)["valid"]


def test_uptrend_alone_is_not_a_pullback_entry():
    now = time.time()
    data = bars(now)
    data[-1] = replace(data[-1], close=1.147, high=1.148)
    reading = assess_trend_pullback(
        data, now=now, pair_created_at_ms=(now - 4 * 86400) * 1000
    )
    assert reading["trend_confirmed"]
    assert not reading["pullback_confirmed"]


def test_handoff_overrides_only_stagnation_and_requires_current_evidence():
    now = time.time()
    p, q, policy = position(now), quote(now), ShadowRecoveryPolicy()
    result = managed_exit_review(p, q, policy, evidence(now), now=now)
    assert result["swing_handoff"] and result["state"] == "HOLD"
    assert managed_exit_review(p, q, policy, {}, now=now)["state"] == "EXIT"
    assert (
        managed_exit_review(p, q, policy, evidence(now - 901), now=now)["state"]
        == "EXIT"
    )
    assert (
        managed_exit_review(p, quote(now, 0.75), policy, evidence(now), now=now)[
            "state"
        ]
        == "EXIT"
    )
    emergency = {**q, "liquidity_usd": 100, "buys_m5": 1, "sells_m5": 30}
    assert (
        managed_exit_review(p, emergency, policy, evidence(now), now=now)["state"]
        == "EMERGENCY_EXIT"
    )
    bad_flow = {**q, "buys_m5": None}
    assert (
        managed_exit_review(p, bad_flow, policy, evidence(now), now=now)["state"]
        == "EXIT"
    )


def test_proactive_handoff_then_invalidation_and_original_deadline():
    now = time.time()
    p = {
        **position(now),
        "opened_at": datetime.fromtimestamp(now - 60, UTC).isoformat(),
    }
    policy = ShadowRecoveryPolicy()
    assert managed_exit_review(p, quote(now), policy, evidence(now), now=now)[
        "swing_handoff"
    ]
    p["exit_manager"] = "swing-v1"
    bad = {**evidence(now), "trend_confirmed": False}
    assert managed_exit_review(p, quote(now), policy, bad, now=now)["state"] == "EXIT"
    p["opened_at"] = datetime.fromtimestamp(now - 25 * 3600, UTC).isoformat()
    assert (
        managed_exit_review(p, quote(now, 1.1), policy, evidence(now), now=now)["state"]
        == "EXIT"
    )


def test_allocation_is_bounded_persistent_and_never_creates_cash(tmp_path):
    book = CapitalBook(tmp_path / "book.json")
    book.initialize(30)
    for i in range(7):
        agent = "hunter-v1" if i < 6 else "copy-v1"
        book.reserve_shadow_buy(
            agent_id=agent,
            mint=str(i),
            symbol="X",
            amount_usd=5,
            entry_price=1,
            price_currency="USD",
            max_open_positions=10,
        )
    before = book.public_status()
    review = {"swing_handoff": True, "evidence_as_of": time.time(), "reasons": ["test"]}
    for i in range(6):
        assert book.delegate_shadow_exit("hunter-v1", str(i), review)
    assert not book.delegate_shadow_exit("copy-v1", "6", review)
    assert book.public_status()["agents"] == before["agents"]
    assert (
        book.public_status()["total_starting_capital_usd"]
        == before["total_starting_capital_usd"]
    )
    reopened = CapitalBook(book.path)
    assert reopened.public_status()["swing_allocation"]["available_usd"] == 0
    assert reopened.delegate_shadow_exit("hunter-v1", "0", review)  # idempotent
    reopened.close_shadow_position("hunter-v1", "0", fraction=0.5)
    assert reopened.public_status()["swing_allocation"]["available_usd"] == 2.5
    reopened.close_shadow_position("hunter-v1", "0")
    assert reopened.public_status()["swing_allocation"]["available_usd"] == 5
    assert reopened.delegate_shadow_exit("copy-v1", "6", review)


def snap(now, mint="A" * 32):
    row = candidate(
        mint=mint,
        price=1,
        peak_price=1.2,
        liquidity_usd=80000,
        pair_created_at_ms=(now - 4 * 86400) * 1000,
        pair_address="pool",
        quoted_at=now,
    )
    return {"generated_at": now, "candidates": [row]}, row


def test_paired_entries_filtered_arm_and_portfolio_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv("ENTRY_ALLOWED_DECISIONS", "BUY ZONE")
    monkeypatch.setenv("ENTRY_MIN_TOKEN_AGE_DAYS", "3")
    exp = ShadowComparison(tmp_path)
    now = time.time()
    snapshot, row = snap(now)
    mint = row["mint"]
    result = exp.cycle(snapshot, {}, {}, now=now)
    assert result["entry"]["arms"] == ["baseline", "managed"]
    assert (
        exp.books["baseline"].load()["agents"]["hunter-v1"]["positions"][mint][
            "entry_price"
        ]
        == 1
    )
    # After six minutes ordinary exits sell; portfolio approves a swing hold.
    later = now + 360
    result = exp.cycle({}, {mint: quote(later)}, {mint: evidence(later)}, now=later)
    assert exp.books["baseline"].load()["agents"]["hunter-v1"]["positions"] == {}
    managed = exp.books["managed"].load()["agents"]["hunter-v1"]["positions"][mint]
    assert managed["exit_manager"] == "swing-v1"
    assert managed["exit_executor"] == "portfolio-v1"
    assert exp.books["managed"].public_status()["swing_allocation"]["reserved_usd"] == 5
    # A hard stop still closes it and releases the full mandate.
    exp.cycle(
        {},
        {mint: quote(later + 60, 0.70)},
        {mint: evidence(later + 60)},
        now=later + 60,
    )
    assert exp.books["managed"].public_status()["swing_allocation"]["reserved_usd"] == 0


def test_filter_only_enters_on_confirmed_pullback_and_deduplicates(tmp_path):
    exp = ShadowComparison(tmp_path)
    now = time.time()
    snapshot, row = snap(now)
    ev = {row["mint"]: evidence(now)}
    assert exp.cycle(snapshot, {}, ev, now=now)["entry"]["arms"] == [
        "baseline",
        "trend",
        "managed",
    ]
    assert exp.cycle(snapshot, {}, ev, now=now)["entry"] is None
    assert ShadowComparison(tmp_path).positions()  # restart preserves positions
    with pytest.raises(ValueError, match="settings changed"):
        ShadowComparison(tmp_path, cost_pct=2)


def test_stale_snapshot_never_opens_positions(tmp_path):
    exp = ShadowComparison(tmp_path)
    now = time.time()
    snapshot, row = snap(now - 60)
    assert (
        exp.cycle(snapshot, {}, {row["mint"]: evidence(now)}, now=now)["entry"] is None
    )


def test_existing_hunter_shadow_loop_uses_manager_and_keeps_risk_exits(
    tmp_path, monkeypatch
):
    import json

    from solana_launch_guard.agent_cli import shadow_once

    now = time.time()
    mint = "M" * 32
    book = CapitalBook(tmp_path / "capital.json")
    book.initialize(30)
    book.reserve_shadow_buy(
        agent_id="hunter-v1",
        mint=mint,
        symbol="M",
        amount_usd=5,
        entry_price=1,
        price_currency="USD",
        entry_liquidity_usd=80000,
    )
    payload = book.load()
    payload["agents"]["hunter-v1"]["positions"][mint]["opened_at"] = position(now)[
        "opened_at"
    ]
    book._write(payload)
    board, proof = tmp_path / "board.json", tmp_path / "evidence.json"
    board.write_text(
        json.dumps(
            {
                "generated_at": now,
                "candidates": [],
                "tracked_candidates": [{"mint": mint, "chain": "solana", **quote(now)}],
            }
        )
    )
    proof.write_text(json.dumps({"tokens": {mint: evidence(now)}}))
    monkeypatch.setenv("RECOMMENDATION_SNAPSHOT_PATH", str(board))
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setenv("AGENT_COPY_SIGNAL_PATH", str(tmp_path / "absent2.json"))
    monkeypatch.setenv("SHADOW_TREND_EVIDENCE_PATH", str(proof))
    monkeypatch.setenv("SWING_SHADOW_MANAGER_ENABLED", "true")
    shadow_once(None, book, core_only=True)
    assert book.public_status()["swing_allocation"]["reserved_usd"] == 5
    board.write_text(
        json.dumps(
            {
                "generated_at": now,
                "candidates": [],
                "tracked_candidates": [
                    {"mint": mint, "chain": "solana", **quote(now, 0.7)}
                ],
            }
        )
    )
    shadow_once(None, book, core_only=True)
    assert book.public_status()["swing_allocation"]["reserved_usd"] == 0


def test_filter_does_not_chase_a_move_after_the_confirmation_candle():
    from solana_launch_guard.trend_pullback import entry_confirmed

    now = time.time()
    assert entry_confirmed(evidence(now), 1, now=now)
    assert not entry_confirmed(evidence(now), 1.2, now=now)
    assert not entry_confirmed(evidence(now), 0.9, now=now)


def test_candle_collector_requests_sufficient_history_and_caches(monkeypatch):
    import asyncio

    from solana_launch_guard.market_structure import MarketStructureScanner

    scanner = MarketStructureScanner()
    calls = []

    def fetch(pool, mint, frame, *, limit):
        calls.append((pool, mint, frame, limit))
        return bars(time.time())

    monkeypatch.setattr(scanner, "_fetch", fetch)
    first = asyncio.run(scanner.trend_candles(pool="P", mint="M"))
    second = asyncio.run(scanner.trend_candles(pool="P", mint="M"))
    assert first == second and len(first) >= 150
    assert calls == [("P", "M", ("minute", 15, 900), 200)]
