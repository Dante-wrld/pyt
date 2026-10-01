"""Dip volume vs rally volume: tracking, labels, the entry gate, and the
evaluation tag that decides whether the gate is worth switching on."""
import math
import sqlite3
import time

from solana_launch_guard.core import SQLiteStore
from solana_launch_guard.hunter_shadow_strategy import (
    ShadowRecoveryPolicy,
    assess_entry,
)
from solana_launch_guard.market import MarketQuote
from solana_launch_guard.outcome_tracker import read_buy_signals
from solana_launch_guard.recommendations import (
    RecommendationBook,
    RecommendationCandidate,
    build_snapshot,
)

MINT = "V" * 32


def board_candidate(**changes):
    c = RecommendationCandidate(
        mint=MINT, symbol="V", chain="solana", tier="CORE",
        intelligence_score=80, initial_price=1.0, current_price=1.0,
        price_currency="USD", liquidity_usd=50_000, initial_liquidity_usd=50_000,
        volume_m5_usd=1_000, initial_volume_m5_usd=1_000, buys_m5=10, sells_m5=5,
        price_change_m5_pct=0, buy_sell_ratio=2, observed_at=0, updated_at=0,
        peak_price=1.0, rally_volume_m5_usd=1_000,
    )
    for key, value in changes.items():
        setattr(c, key, value)
    return c


def feed(book, c, path):
    """Walk (price, m5 volume) polls the way update() does."""
    for price, volume in path:
        book._track_pullback_volume(c, price, volume)
        c.peak_price = max(c.peak_price, price)
        c.current_price = price


def test_light_dip_after_heavy_rally_is_labelled_light():
    book, c = RecommendationBook(), board_candidate()
    feed(book, c, [(1.1, 8_000), (1.2, 10_000), (1.15, 3_000), (1.13, 4_000)])
    assert c.rally_volume_m5_usd == 10_000
    assert c.pullback_max_volume_m5_usd == 4_000
    assert c.pullback_volume_label == "LIGHT"


def test_dip_trading_as_heavily_as_the_rally_is_heavy():
    book, c = RecommendationBook(), board_candidate()
    feed(book, c, [(1.2, 5_000), (1.1, 6_000)])
    assert math.isclose(c.pullback_volume_ratio, 1.2)
    assert c.pullback_volume_label == "HEAVY"


def test_polls_hugging_the_peak_count_as_rally_not_dip():
    book, c = RecommendationBook(pullback_started_pct=2.0), board_candidate()
    feed(book, c, [(1.2, 2_000), (1.19, 9_000)])  # under 1% off the high
    assert c.rally_volume_m5_usd == 9_000
    assert c.pullback_volume_label == "UNKNOWN"


def test_new_high_after_a_dip_starts_a_fresh_leg():
    book, c = RecommendationBook(), board_candidate()
    feed(book, c, [(1.2, 10_000), (1.1, 9_000), (1.3, 2_000), (1.25, 1_600)])
    assert c.rally_volume_m5_usd == 2_000
    assert c.pullback_max_volume_m5_usd == 1_600
    assert c.pullback_volume_label == "NORMAL"


def test_nan_volume_is_ignored():
    book, c = RecommendationBook(), board_candidate()
    feed(book, c, [(1.2, float("nan")), (1.1, float("nan"))])
    assert c.rally_volume_m5_usd == 1_000
    assert c.pullback_volume_label == "UNKNOWN"


def test_update_tracks_through_real_quotes_and_snapshot_exposes_it():
    book = RecommendationBook()
    c = board_candidate()
    book.candidates[c.key] = c

    def quote(price, volume):
        return MarketQuote(
            mint=MINT, symbol="V", price_sol=price, liquidity_usd=50_000,
            market_cap_usd=None, pair_address="P", pair_created_at_ms=1,
            buys_m5=10, sells_m5=5, volume_m5_usd=volume,
            price_change_m5_pct=0, price_usd=price,
        )

    for price, volume in [(1.2, 10_000), (1.12, 3_000)]:
        book.update(quote(price, volume), now=time.time())
    row = build_snapshot([c], pending_count=0, poll_seconds=20)["candidates"][0]
    assert row["pullback_volume_label"] == "LIGHT"
    assert math.isclose(row["pullback_volume_ratio"], 0.3)


def test_old_snapshot_without_the_fields_still_loads():
    c = board_candidate()
    payload = c.to_json().replace('"rally_volume_m5_usd":1000,', "")
    payload = payload.replace('"pullback_max_volume_m5_usd":0.0,', "")
    assert "rally_volume" not in payload
    restored = RecommendationCandidate.from_json(payload)
    assert restored.pullback_volume_label == "UNKNOWN"


def hunter_row(**changes):
    row = {"mint": MINT, "chain": "solana", "symbol": "V", "decision": "BUY ZONE",
           "price": 0.9, "price_currency": "USD", "peak_price": 1.0,
           "pullback_from_peak_pct": 10, "liquidity_usd": 20_000,
           "initial_liquidity_usd": 20_000, "price_change_m5_pct": 3,
           "momentum_label": "RISING", "volume_label": "STEADY",
           "buys_m5": 20, "sells_m5": 10, "buy_sell_ratio": 2,
           "risk_label": "MEDIUM", "signal_score": 80,
           "entry_confirmation_count": 3, "entry_confirmation_required": 3,
           "pullback_volume_ratio": 0.4, "quoted_at": time.time()}
    row.update(changes)
    return row


def test_gate_is_off_by_default():
    policy = ShadowRecoveryPolicy()
    for ratio in (3.0, None):
        review = assess_entry(hunter_row(pullback_volume_ratio=ratio), policy)
        assert review["state"] == "BUY_READY"


def test_gate_blocks_heavy_and_unmeasured_dips_when_enabled():
    policy = ShadowRecoveryPolicy(max_pullback_volume_ratio=1.0)
    assert assess_entry(hunter_row(), policy)["state"] == "BUY_READY"
    for ratio in (1.0, 2.5, None, float("nan"), True):
        review = assess_entry(hunter_row(pullback_volume_ratio=ratio), policy)
        assert review["state"] != "BUY_READY", ratio
        assert "pullback_volume" in review["failure_codes"]


def test_gate_leaves_momentum_and_early_buys_alone():
    policy = ShadowRecoveryPolicy(max_pullback_volume_ratio=1.0)
    momentum = hunter_row(decision="MOMENTUM BUY", buy_sell_ratio=2, buys_m5=40,
                          sells_m5=20, pullback_volume_ratio=None,
                          entry_confirmation_required=1)
    assert "pullback_volume" not in assess_entry(momentum, policy)["failure_codes"]
    early = hunter_row(decision="EARLY BUY", pullback_volume_ratio=5.0)
    assert "pullback_volume" not in assess_entry(early, policy)["failure_codes"]


def test_env_setting_reaches_the_policy(monkeypatch):
    monkeypatch.setenv("BUY_ZONE_MAX_PULLBACK_VOLUME_RATIO", "0.8")
    assert ShadowRecoveryPolicy.from_env().max_pullback_volume_ratio == 0.8


def test_signal_tag_flows_into_evaluation(tmp_path):
    database = tmp_path / "g.db"
    store = SQLiteStore(str(database))
    store.save_buy_signal(
        mint=MINT, symbol="V", chain="solana", decision="BUY ZONE", price=1.0,
        price_currency="USD", liquidity_usd=50_000, signal_score=80,
        pair_created_at_ms=1, reason="r", live_blocked_reason=None,
        sources="board", pullback_volume="LIGHT",
    )
    [decision] = read_buy_signals(database, 0)
    assert "pullback_volume: LIGHT" in decision.reasons


def test_databases_from_before_the_column_still_read(tmp_path):
    database = tmp_path / "old.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE buy_signals (id INTEGER PRIMARY KEY, signaled_at TEXT, "
        "mint TEXT, chain TEXT, decision TEXT, reason TEXT, live_blocked_reason TEXT)"
    )
    connection.execute(
        "INSERT INTO buy_signals VALUES (1, '2026-09-30T00:00:00+00:00', ?, "
        "'solana', 'BUY ZONE', 'r', NULL)", (MINT,),
    )
    connection.commit()
    connection.close()
    [decision] = read_buy_signals(database, 0)
    assert not any(r.startswith("pullback_volume: ") for r in decision.reasons)
