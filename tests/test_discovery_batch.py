"""The auto-buy discovery monitor prices all due watches in one batched request."""
import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from solana_launch_guard.app import LaunchGuard
from solana_launch_guard.core import SQLiteStore
from solana_launch_guard.market import MarketQuote

from test_core import launch_payload, settings

MINTS = ["MintBatchA11", "MintBatchB22", "MintBatchC33"]


class StopLoop(Exception):
    pass


def _quote(mint: str) -> MarketQuote:
    return MarketQuote(
        mint=mint, symbol=mint[-3:], price_sol=0.000001, liquidity_usd=60_000,
        market_cap_usd=100_000, pair_address="P" + mint, pair_created_at_ms=1,
        buys_m5=60, sells_m5=20, volume_m5_usd=15_000, price_change_m5_pct=5,
    )


class FakeOracle:
    def __init__(self, *, batch_fails: bool = False, batch_misses: tuple = ()):
        self.single: list[str] = []
        self.batches: list[list[str]] = []
        self.batch_fails = batch_fails
        self.batch_misses = batch_misses

    async def quote(self, mint, *, chain="solana"):
        self.single.append(mint)
        return _quote(mint)

    async def quote_many(self, mints, *, chain="solana"):
        self.batches.append(list(mints))
        if self.batch_fails:
            raise RuntimeError("batch endpoint down")
        return {m: (None if m in self.batch_misses else _quote(m)) for m in mints}

    async def sol_usd_price(self):
        return 100.0


def _guard_with_due_watches(tmp_path: Path, oracle: FakeOracle):
    database = tmp_path / "discovery.db"
    config = replace(
        settings(database, auto_buy_enabled=True, auto_buy_discovery=True,
                 auto_buy_watch_max_seconds=3600, auto_buy_watch_batch_size=10,
                 auto_buy_watch_retry_base_seconds=5),
        intelligence_wait_seconds=0.0,  # first check is due immediately
    )
    store = SQLiteStore(str(database))
    guard = LaunchGuard(config, store)
    guard.oracle = oracle  # type: ignore[assignment]
    for mint in MINTS:
        asyncio.run(guard.handle_launch(launch_payload(mint=mint, symbol=mint[-3:])))
    return store, guard


def _run_one_iteration(guard, monkeypatch):
    async def stop(_seconds):
        raise StopLoop

    monkeypatch.setattr("asyncio.sleep", stop)
    with pytest.raises(StopLoop):
        asyncio.run(guard.run_auto_buy_discovery_monitor())


def test_all_due_watches_are_priced_in_one_batched_request(tmp_path, monkeypatch):
    oracle = FakeOracle()
    store, guard = _guard_with_due_watches(tmp_path, oracle)
    _run_one_iteration(guard, monkeypatch)
    assert len(oracle.batches) == 1 and sorted(oracle.batches[0]) == sorted(MINTS)
    assert oracle.single == []  # no per-watch request at all
    assert all(store.load_auto_buy_discovery_watch(m)["attempts"] == 1 for m in MINTS)
    store.close()


def test_a_watch_the_batch_missed_falls_back_to_its_own_lookup(tmp_path, monkeypatch):
    oracle = FakeOracle(batch_misses=())
    store, guard = _guard_with_due_watches(tmp_path, oracle)
    watch = store.load_auto_buy_discovery_watch(MINTS[0])
    asyncio.run(guard._evaluate_auto_buy_discovery_watch(watch, prefetched={}))
    assert oracle.single == [MINTS[0]]  # not in the prefetched map: priced itself
    asyncio.run(guard._evaluate_auto_buy_discovery_watch(
        store.load_auto_buy_discovery_watch(MINTS[1]),
        prefetched={MINTS[1]: _quote(MINTS[1])}))
    assert oracle.single == [MINTS[0]]  # prefetched: no extra request
    store.close()


def test_a_failed_batch_falls_back_to_single_lookups(tmp_path, monkeypatch):
    oracle = FakeOracle(batch_fails=True)
    store, guard = _guard_with_due_watches(tmp_path, oracle)
    _run_one_iteration(guard, monkeypatch)
    assert sorted(oracle.single) == sorted(MINTS)  # exactly the old behavior
    store.close()
