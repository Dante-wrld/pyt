"""wide-v1: paper-only book for the signals the freeze keeps out (2026-09-27)."""
import asyncio
import json
import time

from solana_launch_guard.book_comparison import load_books, wide_slices
from solana_launch_guard.hunter_shadow_strategy import ShadowRecoveryPolicy
from solana_launch_guard.wide_shadow import (
    WIDE_AGENT_ID,
    WideCapitalBook,
    wide_cycle,
    wide_decisions,
)

from test_hunter_shadow_strategy import candidate

DAY_MS = 86_400_000
POLICY = ShadowRecoveryPolicy()


def _snapshot(rows):
    now = time.time()
    for row in rows:
        row["quoted_at"] = now
    return json.loads(json.dumps({"generated_at": now, "candidates": rows}))


def _book(tmp_path):
    book = WideCapitalBook(tmp_path / "wide.json")
    book.initialize(30)
    return book


def _run(book, snapshot, quotes=None, **kwargs):
    async def fetch(mints):
        return {m: q for m, q in (quotes or {}).items() if m in mints}

    return asyncio.run(wide_cycle(book, snapshot, policy=POLICY,
                                  fetch_quotes=fetch, **kwargs))


def _frozen(monkeypatch):
    monkeypatch.setenv("ENTRY_ALLOWED_DECISIONS", "BUY ZONE")
    monkeypatch.setenv("ENTRY_MIN_TOKEN_AGE_DAYS", "3")


def test_takes_what_the_freeze_blocks_and_records_why(tmp_path, monkeypatch):
    _frozen(monkeypatch)
    young = time.time() * 1000 - DAY_MS
    book = _book(tmp_path)
    result = _run(book, _snapshot([
        candidate(mint="Y" * 44, decision="BUY ZONE", pair_created_at_ms=young)]))
    assert result["entry"]["mint"] == "Y" * 44
    assert "ENTRY_MIN_TOKEN_AGE_DAYS" in result["entry"]["live_blocked"]
    position = book.load()["agents"][WIDE_AGENT_ID]["positions"]["Y" * 44]
    assert position["decision"] == "BUY ZONE" and position["live_blocked"]


def test_marks_trades_live_would_also_take(tmp_path, monkeypatch):
    _frozen(monkeypatch)
    old = time.time() * 1000 - 10 * DAY_MS
    book = _book(tmp_path)
    result = _run(book, _snapshot([candidate(mint="O" * 44, pair_created_at_ms=old)]))
    assert result["entry"]["live_blocked"] is None


def test_skips_paused_momentum_and_non_ready(tmp_path, monkeypatch):
    _frozen(monkeypatch)
    book = _book(tmp_path)
    result = _run(book, _snapshot([
        candidate(mint="M" * 44, decision="MOMENTUM BUY"),
        candidate(mint="F" * 44, momentum_label="FALLING", price_change_m5_pct=-9),
    ]))
    assert result["entry"] is None


def test_four_slots_then_full(tmp_path):
    book = _book(tmp_path)
    for i in range(5):
        _run(book, _snapshot([candidate(mint=str(i) * 44, signal_score=90)]))
    assert len(book.load()["agents"][WIDE_AGENT_ID]["positions"]) == 4


def test_exits_with_hunter_rules_off_the_board(tmp_path):
    book = _book(tmp_path)
    _run(book, _snapshot([candidate(mint="E" * 44)]))       # bought at 0.9
    result = _run(book, _snapshot([]), quotes={
        "E" * 44: {"price": 0.6, "price_currency": "USD", "liquidity_usd": 20_000}})
    [review] = result["exits"]
    assert review["state"] == "EXIT" and "hard stop" in " ".join(review["reasons"])
    fill = book.load()["agents"][WIDE_AGENT_ID]["completed_trades"][0]
    assert fill["decision"] == "BUY ZONE" and fill["slippage_pct"] == 1.2


def test_decisions_are_configurable(monkeypatch):
    assert "MOMENTUM BUY" not in wide_decisions()
    monkeypatch.setenv("WIDE_DECISIONS", "buy now, momentum buy")
    assert wide_decisions() == ("BUY NOW", "MOMENTUM BUY")


def test_comparison_splits_the_wide_book(tmp_path, monkeypatch):
    _frozen(monkeypatch)
    old = time.time() * 1000 - 10 * DAY_MS
    young = time.time() * 1000 - DAY_MS
    book = _book(tmp_path)
    _run(book, _snapshot([candidate(mint="A" * 44, pair_created_at_ms=old)]))
    _run(book, _snapshot([candidate(mint="B" * 44, pair_created_at_ms=young)]))
    for mint, price in (("A" * 44, 1.2), ("B" * 44, 0.8)):
        book.mark_shadow_position(WIDE_AGENT_ID, mint, price)
        book.close_shadow_position(WIDE_AGENT_ID, mint)
    rows = {r.name: r for r in wide_slices(book.load()["agents"][WIDE_AGENT_ID])}
    assert rows["wide-v1"].trades == 2
    assert rows["wide:live-too"].net_usd > 0 > rows["wide:frozen-out"].net_usd
    assert rows["wide:BUY ZONE"].trades == 2
    names = [b.name for b in load_books(
        swing_book=tmp_path / "none.json", trend_directory=tmp_path / "none",
        hunter_book=None, wide_book=tmp_path / "wide.json")]
    assert "wide:frozen-out" in names
