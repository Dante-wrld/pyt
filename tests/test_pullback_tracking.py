from __future__ import annotations

import json

from solana_launch_guard.pullback_tracking import PullbackTracker
from solana_launch_guard.recommendations import RecommendationBook, RecommendationCandidate


def candidate(now: float) -> RecommendationCandidate:
    return RecommendationCandidate(
        mint="A" * 32, symbol="TEST", chain="solana", tier="CORE",
        intelligence_score=90, initial_price=100, current_price=95,
        price_currency="USD", liquidity_usd=60_000,
        initial_liquidity_usd=60_000, volume_m5_usd=100,
        initial_volume_m5_usd=100, buys_m5=10, sells_m5=2,
        price_change_m5_pct=-1, buy_sell_ratio=5, observed_at=now,
        updated_at=now, peak_price=100, entry_zone_low=94,
        entry_zone_high=96, decision="WATCH",
        decision_reason="in range, but recovery is unconfirmed",
        entry_confirmation_required=3,
    )


def test_pullback_history_survives_rank_changes_and_restart(tmp_path) -> None:
    now = 1000.0
    book = RecommendationBook(ttl_seconds=1800)
    c = candidate(now)
    book.candidates[c.key] = c
    tracker = PullbackTracker(tmp_path / "recommendations.json")
    tracker.record(book, now=now)
    c.current_price = 93
    c.updated_at = now + 1
    c.decision_reason = "price fell through the entry zone"
    tracker.record(book, now=now + 1)
    events = [json.loads(line) for line in tracker.history_path.read_text().splitlines()]
    assert [event["state"] for event in events] == [
        "in_zone_unconfirmed", "fell_through_zone"
    ]
    restarted = RecommendationBook(ttl_seconds=1800)
    assert PullbackTracker(tmp_path / "recommendations.json").restore(
        restarted, now=now + 2
    ) == 1
    assert restarted.candidates[c.key].current_price == 93


def test_stale_pullback_is_not_reactivated(tmp_path) -> None:
    book = RecommendationBook(ttl_seconds=10)
    c = candidate(1000)
    book.candidates[c.key] = c
    tracker = PullbackTracker(tmp_path / "recommendations.json")
    tracker.record(book, now=1000)
    restarted = RecommendationBook(ttl_seconds=10)
    assert tracker.restore(restarted, now=1011) == 0
    assert not restarted.candidates


def test_missing_pool_candidate_has_unknown_outcome(tmp_path) -> None:
    book = RecommendationBook()
    c = candidate(1000)
    book.candidates[c.key] = c
    tracker = PullbackTracker(tmp_path / "recommendations.json")
    tracker.record(book, now=1000)
    del book.candidates[c.key]
    tracker.record(book, now=1001)
    last = json.loads(tracker.history_path.read_text().splitlines()[-1])
    assert last["state"] == "left_tracking_pool"
    assert "unknown" in last["reason"]
