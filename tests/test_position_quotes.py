"""One shared price lookup for every paper book's held tokens."""
import asyncio
import json
import time
import urllib.error
from types import SimpleNamespace

from solana_launch_guard import swing_strategy
from solana_launch_guard.position_quotes import (
    held_mints,
    read_shared_quotes,
    refresh_quotes,
    write_shared_quotes,
)

A, B, C = "A" * 44, "B" * 44, "C" * 44


def _book(path, agent, mints):
    positions = {m: {} for m in mints}
    path.write_text(json.dumps({"agents": {agent: {"positions": positions}}}))
    return path


def _q(price, liquidity=50_000.0):
    return SimpleNamespace(price_usd=price, liquidity_usd=liquidity)


async def _no_sleep(_seconds):
    return None


def test_held_mints_is_the_union_across_books_and_skips_missing_ones(tmp_path):
    one = _book(tmp_path / "one.json", "wide-v1", [A, B])
    two = _book(tmp_path / "two.json", "wide-fresh-v1", [B, C])
    assert held_mints([one, two, tmp_path / "absent.json"]) == [A, B, C]


def test_shared_quotes_ignore_stale_and_unpriced_entries(tmp_path):
    path = tmp_path / "q.json"
    now = time.time()
    write_shared_quotes(path, {
        A: {"price": 1.0, "price_currency": "USD", "quoted_at": now - 5},
        B: {"price": 1.0, "price_currency": "USD", "quoted_at": now - 120},
        C: {"price": 0, "price_currency": "USD", "quoted_at": now},
    }, now=now)
    assert set(read_shared_quotes(path, now=now)) == {A}
    assert read_shared_quotes(tmp_path / "nope.json") == {}


def test_refresh_prices_every_mint_in_batches_with_one_request_each():
    calls = []

    async def fetch(batch):
        calls.append(list(batch))
        return {m: _q(2.0) for m in batch}

    out = asyncio.run(refresh_quotes([A, B, C], fetch, batch_size=2, now=100.0))
    assert calls == [[A, B], [C]]
    assert out[A] == {"price": 2.0, "price_currency": "USD",
                      "liquidity_usd": 50_000.0, "quoted_at": 100.0}


def test_a_429_is_retried_instead_of_losing_the_cycle():
    attempts = []

    async def fetch(batch):
        attempts.append(1)
        if len(attempts) < 3:
            raise urllib.error.HTTPError("u", 429, "Too Many Requests", None, None)
        return {m: _q(1.5) for m in batch}

    out = asyncio.run(refresh_quotes([A], fetch, now=1.0, sleep=_no_sleep))
    assert len(attempts) == 3 and out[A]["price"] == 1.5


def test_a_failing_batch_keeps_its_previous_price_and_drops_unheld_mints():
    async def fetch(batch):
        raise RuntimeError("boom")

    previous = {A: {"price": 1.0, "quoted_at": 5.0},
                C: {"price": 9.0, "quoted_at": 5.0}}
    out = asyncio.run(refresh_quotes([A, B], fetch, previous=previous, now=50.0))
    assert out == {A: {"price": 1.0, "quoted_at": 5.0}}  # C no longer held


def test_books_use_the_shared_file_and_fetch_only_what_it_lacks(
    tmp_path, monkeypatch
):
    path = tmp_path / "q.json"
    now = time.time()
    write_shared_quotes(path, {A: {"price": 3.0, "price_currency": "USD",
                                   "liquidity_usd": 1.0, "quoted_at": now}}, now=now)
    monkeypatch.setenv("SHARED_QUOTES_PATH", str(path))
    asked = []

    class Client:
        async def quotes(self, batch):
            asked.append(list(batch))
            return {m: _q(7.0) for m in batch}

    import solana_launch_guard.outcome_tracker as outcome_tracker
    monkeypatch.setattr(outcome_tracker, "DexScreenerBatchClient", Client)
    out = asyncio.run(swing_strategy.dexscreener_quotes([A, B]))
    assert out[A]["price"] == 3.0 and out[B]["price"] == 7.0
    assert asked == [[B]]  # A came from the shared file


def test_a_failed_direct_request_keeps_the_shared_prices(tmp_path, monkeypatch):
    path = tmp_path / "q.json"
    now = time.time()
    write_shared_quotes(path, {A: {"price": 3.0, "price_currency": "USD",
                                   "liquidity_usd": 1.0, "quoted_at": now}}, now=now)
    monkeypatch.setenv("SHARED_QUOTES_PATH", str(path))

    class Broken:
        async def quotes(self, batch):
            raise urllib.error.HTTPError("u", 429, "Too Many Requests", None, None)

    import solana_launch_guard.outcome_tracker as outcome_tracker
    monkeypatch.setattr(outcome_tracker, "DexScreenerBatchClient", Broken)
    assert set(asyncio.run(swing_strategy.dexscreener_quotes([A, B]))) == {A}
    # nothing shared and the direct request fails: the caller still sees it
    monkeypatch.setenv("SHARED_QUOTES_PATH", str(tmp_path / "none.json"))
    try:
        asyncio.run(swing_strategy.dexscreener_quotes([B]))
    except urllib.error.HTTPError:
        pass
    else:
        raise AssertionError("expected the failure to propagate")
