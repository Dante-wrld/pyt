import asyncio
import json
import time

import pytest
from solana_launch_guard.app import LaunchGuard
from solana_launch_guard.core import SQLiteStore
from solana_launch_guard.launch_guard_copy_signals import (
    SOURCE,
    CopySignalConfig,
    PendingCopySignal,
    build_copy_signals,
    prune_expired,
)
from solana_launch_guard.market import MarketQuote
from solana_launch_guard.strategy_profile import StrategyProfile
from solana_launch_guard.wallet import WalletTrade

from test_core import settings

A = "A" * 44
B = "B" * 44
NOW = 1_000_000_000.0
CONFIG = CopySignalConfig(poll_seconds=5, ttl_seconds=1800, min_usd=50,
                          snapshot_path="unused.json")


def sig(mint, *, leader="ryantrost", entry=1.0, detected_at=NOW):
    return PendingCopySignal(leader=leader, leader_wallet="addr1", mint=mint,
                              entry_price_usd=entry, detected_at=detected_at)


def quote(mint, *, price=1.0, liquidity=80_000.0, symbol=None):
    return MarketQuote(
        mint=mint, symbol=symbol or mint[:4], price_sol=price / 150, price_usd=price,
        liquidity_usd=liquidity, market_cap_usd=200_000, pair_address="P" + mint[:4],
        pair_created_at_ms=1, buys_m5=60, sells_m5=20, volume_m5_usd=15_000,
        price_change_m5_pct=5,
    )


# --- pure functions ----------------------------------------------------------

def test_prune_expired_drops_only_what_outlived_the_ttl():
    pending = {A: sig(A, detected_at=NOW - 100), B: sig(B, detected_at=NOW - 5000)}
    kept = prune_expired(pending, now=NOW, ttl_seconds=1800)
    assert list(kept) == [A]


def test_build_copy_signals_computes_the_move_since_entry():
    pending = {A: sig(A, entry=1.0)}
    signals = build_copy_signals(pending, {A: quote(A, price=1.5)}, now=NOW)
    assert signals[0]["price_move_since_entry_pct"] == pytest.approx(50.0)
    assert signals[0]["leader"] == "ryantrost"
    assert signals[0]["mint"] == A


def test_build_copy_signals_skips_unpriced_quotes():
    pending = {A: sig(A)}
    assert build_copy_signals(pending, {A: None}, now=NOW) == []
    assert build_copy_signals(
        pending, {A: quote(A, price=0)}, now=NOW
    ) == []


def test_build_copy_signals_skips_stock_tokens():
    pending = {A: sig(A)}
    signals = build_copy_signals(
        pending, {A: quote(A, symbol="TSLAx")}, now=NOW,
        stock_symbols=frozenset({"tsla"}),
    )
    assert signals == []


def test_an_entry_price_of_zero_reports_zero_move_instead_of_dividing_by_it():
    pending = {A: sig(A, entry=0.0)}
    signals = build_copy_signals(pending, {A: quote(A, price=2.0)}, now=NOW)
    assert signals[0]["price_move_since_entry_pct"] == 0.0


# --- CopySignalFeedMixin, with a real LaunchGuard -----------------------------

class FakeOracle:
    def __init__(self, quotes):
        self.quotes = quotes

    async def quote(self, mint, *, chain="solana"):
        return self.quotes.get(mint)

    async def quote_many(self, mints, *, chain="solana"):
        return {m: self.quotes.get(m) for m in mints}

    async def robinhood_stock_token_symbols(self):
        return frozenset()


def _guard(tmp_path, **overrides):
    database = tmp_path / "g.db"
    store = SQLiteStore(str(database))
    return store, LaunchGuard(settings(database, **overrides), store)


def test_record_leader_buy_stores_a_pending_signal(tmp_path, monkeypatch):
    monkeypatch.delenv("COPY_SIGNAL_MIN_USD", raising=False)
    store, guard = _guard(tmp_path)
    guard.record_leader_buy("ryantrost", "addr1", A, 1.0, 100.0)
    assert A in guard.copy_signals
    assert guard.copy_signals[A].leader == "ryantrost"
    store.close()


def test_record_leader_buy_drops_dust_below_min_usd(tmp_path, monkeypatch):
    monkeypatch.setenv("COPY_SIGNAL_MIN_USD", "50")
    store, guard = _guard(tmp_path)
    guard.record_leader_buy("ryantrost", "addr1", A, 1.0, 10.0)
    assert A not in guard.copy_signals
    store.close()


def test_poll_adds_to_board_tagged_and_writes_a_fresh_snapshot(tmp_path):
    store, guard = _guard(tmp_path)
    guard.oracle = FakeOracle({A: quote(A)})
    guard.copy_signals[A] = sig(A, entry=0.8, detected_at=time.time())
    path = tmp_path / "copy_signals.json"
    config = CopySignalConfig(poll_seconds=5, ttl_seconds=1800, min_usd=50,
                              snapshot_path=str(path))
    signals = asyncio.run(guard.poll_copy_signals(config))
    assert signals[0]["mint"] == A
    board = guard.recommendations.candidates[f"solana:{A}"]
    assert board.sources == [SOURCE]
    written = json.loads(path.read_text())
    assert written["signals"][0]["mint"] == A
    assert 0 <= time.time() - written["generated_at"] <= 5
    store.close()


def test_poll_prunes_expired_signals_and_writes_an_empty_snapshot(tmp_path):
    store, guard = _guard(tmp_path)
    guard.oracle = FakeOracle({})
    guard.copy_signals[A] = sig(A, detected_at=time.time() - 10_000)
    path = tmp_path / "copy_signals.json"
    config = CopySignalConfig(poll_seconds=5, ttl_seconds=1800, min_usd=50,
                              snapshot_path=str(path))
    signals = asyncio.run(guard.poll_copy_signals(config))
    assert signals == []
    assert A not in guard.copy_signals
    assert json.loads(path.read_text())["signals"] == []
    store.close()


def test_poll_skips_a_now_unpriced_signal_without_crashing(tmp_path):
    store, guard = _guard(tmp_path)
    guard.oracle = FakeOracle({A: None})
    guard.copy_signals[A] = sig(A, detected_at=time.time())
    path = tmp_path / "copy_signals.json"
    config = CopySignalConfig(poll_seconds=5, ttl_seconds=1800, min_usd=50,
                              snapshot_path=str(path))
    signals = asyncio.run(guard.poll_copy_signals(config))
    assert signals == []
    store.close()


# --- wiring: CopyFomoMonitorMixin's BUY path feeds the copy-signal feed ------

def _buy(wallet, mint, amount):
    return WalletTrade(wallet=wallet, signature=f"sig-{amount}", slot=1, mint=mint,
                       side="BUY", token_delta=amount, native_sol_delta=0.0,
                       usdc_delta=50.0)


def test_a_leaders_buy_is_recorded_as_a_copy_signal(tmp_path, monkeypatch):
    monkeypatch.setenv("COPY_SIGNAL_MIN_USD", "50")
    store, guard = _guard(tmp_path, copyfomo_leader_wallets=(("ryantrost", B),))
    guard.oracle = FakeOracle({A: quote(A, price=2.0)})
    asyncio.run(guard.handle_copyfomo_solana_trade(_buy(B, A, 100)))  # $200
    assert A in guard.copy_signals
    assert guard.copy_signals[A].leader == "ryantrost"
    assert guard.copy_signals[A].entry_price_usd == pytest.approx(2.0)
    store.close()


def test_a_tiny_leader_buy_is_not_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("COPY_SIGNAL_MIN_USD", "50")
    store, guard = _guard(tmp_path, copyfomo_leader_wallets=(("ryantrost", B),))
    guard.oracle = FakeOracle({A: quote(A, price=2.0)})
    asyncio.run(guard.handle_copyfomo_solana_trade(_buy(B, A, 1)))  # $2
    assert A not in guard.copy_signals
    store.close()


def test_a_buy_from_an_unwatched_wallet_is_not_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("COPY_SIGNAL_MIN_USD", "50")
    store, guard = _guard(tmp_path, copyfomo_leader_wallets=(("ryantrost", B),))
    guard.oracle = FakeOracle({A: quote(A, price=2.0)})
    asyncio.run(guard.handle_copyfomo_solana_trade(_buy("someone-else", A, 1_000)))
    assert A not in guard.copy_signals
    store.close()


# --- shadow-only protection ---------------------------------------------------

def test_leader_copy_is_shadow_only_by_default():
    profile = StrategyProfile(entry_allowed_decisions=("BUY ZONE",))
    assert "shadow-only source leader-copy" in profile.entry_block_reason(
        "BUY ZONE", 1, sources=["leader-copy"]
    )


def test_env_default_includes_both_leader_sources(monkeypatch):
    monkeypatch.delenv("ENTRY_SHADOW_ONLY_SOURCES", raising=False)
    profile = StrategyProfile.from_env()
    assert set(profile.entry_shadow_only_sources) == {"leader-held", "leader-copy"}
