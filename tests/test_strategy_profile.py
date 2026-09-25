import asyncio
import time
from pathlib import Path

import pytest
from solana_launch_guard.app import LaunchGuard
from solana_launch_guard.core import SQLiteStore
from solana_launch_guard.intelligence import CoinIntelligence
from solana_launch_guard.live_trial_ledger import LiveTrialLedger
from solana_launch_guard.live_trial_runner import decide_hunter_entry
from solana_launch_guard.recommendations import (
    RecommendationBook,
    RecommendationCandidate,
    build_snapshot,
)
from solana_launch_guard.strategy_profile import ALL_BUY_DECISIONS, StrategyProfile

from test_core import market_quote, settings
from test_live_trial_runner import MINT, Model, snapshot

DAY_MS = 86_400_000
PROFILE_KEYS = (
    "FEED_PUMPPORTAL_LAUNCHES", "FEED_LAUNCHLAB", "FEED_SOLANA_MOMENTUM",
    "FEED_COPYFOMO_WALLETS", "FEED_MULTICHAIN", "ENTRY_ALLOWED_DECISIONS",
    "ENTRY_MIN_TOKEN_AGE_DAYS",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in PROFILE_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_defaults_preserve_previous_behaviour():
    profile = StrategyProfile.from_env()
    assert profile == StrategyProfile()
    for decision in ALL_BUY_DECISIONS:
        assert profile.entry_block_reason(decision, None) is None


def test_recommended_profile_parses(monkeypatch):
    monkeypatch.setenv("FEED_PUMPPORTAL_LAUNCHES", "true")
    monkeypatch.setenv("FEED_LAUNCHLAB", "false")
    monkeypatch.setenv("FEED_COPYFOMO_WALLETS", "false")
    monkeypatch.setenv("FEED_MULTICHAIN", "false")
    monkeypatch.setenv("ENTRY_ALLOWED_DECISIONS", "buy zone")
    monkeypatch.setenv("ENTRY_MIN_TOKEN_AGE_DAYS", "3")
    profile = StrategyProfile.from_env()
    assert profile.entry_allowed_decisions == ("BUY ZONE",)
    assert not profile.feed_launchlab and profile.feed_solana_momentum
    assert "live entries: BUY ZONE on tokens 3d+" in profile.describe()


def test_unknown_decision_is_rejected_loudly(monkeypatch):
    monkeypatch.setenv("ENTRY_ALLOWED_DECISIONS", "BUY ZONE,BUY THE DIP")
    with pytest.raises(ValueError, match="BUY THE DIP"):
        StrategyProfile.from_env()


def test_gate_blocks_decision_type_and_young_or_unknown_age():
    profile = StrategyProfile(
        entry_allowed_decisions=("BUY ZONE",), entry_min_token_age_days=3
    )
    now = 100 * 86_400.0
    old = now * 1000 - 5 * DAY_MS
    young = now * 1000 - 1 * DAY_MS
    assert profile.entry_block_reason("BUY ZONE", old, now=now) is None
    assert "shadow-only" in profile.entry_block_reason("MOMENTUM BUY", old, now=now)
    assert "1.0d old" in profile.entry_block_reason("BUY ZONE", young, now=now)
    assert "unknown" in profile.entry_block_reason("BUY ZONE", None, now=now)


def test_candidate_carries_token_age_into_snapshot():
    quote = market_quote(
        liquidity=50_000, market_cap=100_000, buys=60, sells=20,
        volume=15_000, change=15,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60)
    book.add(quote, CoinIntelligence().score(quote), now=0)
    [row] = build_snapshot(book.ranked(), pending_count=0, poll_seconds=15)[
        "candidates"
    ]
    assert row["pair_created_at_ms"] == quote.pair_created_at_ms


def test_old_saved_candidates_still_load():
    fields = RecommendationCandidate(
        mint="m", symbol="s", chain="solana", tier="CORE", intelligence_score=1,
        initial_price=1, current_price=1, price_currency="SOL",
        liquidity_usd=1, initial_liquidity_usd=1, volume_m5_usd=1,
        initial_volume_m5_usd=1, buys_m5=1, sells_m5=1, price_change_m5_pct=0,
        buy_sell_ratio=1, observed_at=0, updated_at=0,
    ).to_json().replace(',"pair_created_at_ms":null', "")
    assert "pair_created_at_ms" not in fields
    assert RecommendationCandidate.from_json(fields).pair_created_at_ms is None


def _early_buy_candidate(now: float):
    quote = market_quote(
        liquidity=60_000, market_cap=100_000, buys=60, sells=20,
        volume=15_000, change=5,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60)
    candidate = book.add(quote, CoinIntelligence().score(quote), now=now)
    book.update(quote, now=now + 5)
    book.update(quote, now=now + 10)
    candidate.updated_at = time.time()
    assert candidate.decision == "EARLY BUY"
    return candidate


def _discovery_guard(tmp_path: Path, name: str):
    store = SQLiteStore(str(tmp_path / name))
    config = settings(
        tmp_path / name, auto_buy_enabled=True, auto_buy_discovery=True,
        auto_buy_discovery_min_score=70,
        auto_buy_discovery_min_liquidity_usd=50_000,
        auto_buy_signal_max_age_seconds=30,
    )
    return store, LaunchGuard(config, store)


def test_auto_buyer_does_not_arm_a_shadow_only_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("ENTRY_ALLOWED_DECISIONS", "BUY ZONE")
    store, guard = _discovery_guard(tmp_path, "shadow.db")
    candidate = _early_buy_candidate(1_000.0)
    asyncio.run(guard._maybe_auto_buy(candidate))
    assert store.load_auto_buy_policy(candidate.mint) is None
    assert "shadow-only" in guard.entry_block_logged[candidate.mint]
    store.close()


def test_auto_buyer_does_not_arm_a_young_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ENTRY_MIN_TOKEN_AGE_DAYS", "3")
    store, guard = _discovery_guard(tmp_path, "young.db")
    candidate = _early_buy_candidate(1_000.0)
    candidate.pair_created_at_ms = int(time.time() * 1000)  # brand new
    asyncio.run(guard._maybe_auto_buy(candidate))
    assert store.load_auto_buy_policy(candidate.mint) is None
    store.close()


def test_auto_buyer_still_arms_an_allowed_signal(tmp_path, monkeypatch):
    monkeypatch.setenv("ENTRY_ALLOWED_DECISIONS", "EARLY BUY")
    monkeypatch.setenv("ENTRY_MIN_TOKEN_AGE_DAYS", "3")
    store, guard = _discovery_guard(tmp_path, "allowed.db")
    candidate = _early_buy_candidate(1_000.0)  # pair_created_at_ms=1: very old
    asyncio.run(guard._maybe_auto_buy(candidate))
    assert store.load_auto_buy_policy(candidate.mint)["armed"] == 1
    store.close()


def test_agent_trial_skips_shadow_only_signal_without_calling_model(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    monkeypatch.setenv("ENTRY_ALLOWED_DECISIONS", "BUY ZONE")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    model = Model(requested=5)
    skips: dict[str, str] = {}
    result = asyncio.run(decide_hunter_entry(
        snapshot(decision="BUY NOW"), model=model, ledger=book,
        buy_zone_skip_reasons=skips,
    ))
    assert result is None
    assert model.calls == 0
    assert "shadow-only" in skips[MINT]
    book.close()


def test_agent_trial_unchanged_when_signal_is_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    monkeypatch.setenv("ENTRY_ALLOWED_DECISIONS", "BUY NOW")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    model = Model(requested=5)
    decision = asyncio.run(
        decide_hunter_entry(snapshot(decision="BUY NOW"), model=model, ledger=book)
    )
    assert decision is not None and decision.approved_cents == 500
    book.close()
