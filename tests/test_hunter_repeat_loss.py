"""Re-entry after repeated losses, shared by the live trial and the paper
hunter (2026-09-27). Two losing sells in a row block a mint until its price
reclaims the entry of the first losing trade - then it "graduates" and may
be bought again. Prompted by hunter-v1 re-buying SDOG and 98kfF7 four times
each, losing every time."""
import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from solana_launch_guard.agent_capital import CapitalBook
from solana_launch_guard.agent_cli import shadow_once
from solana_launch_guard.hunter_shadow_strategy import SellRecord, reentry_block_reason

from test_hunter_shadow_strategy import MINT as READY_MINT
from test_hunter_shadow_strategy import candidate

MINT = "S" * 44


def _book(tmp_path):
    book = CapitalBook(tmp_path / "capital.json")
    book.initialize(30)
    return book


def _round_trip(book, mint, entry, exit_price):
    book.reserve_shadow_buy(agent_id="hunter-v1", mint=mint, symbol="S", amount_usd=5,
                            entry_price=entry, price_currency="USD")
    book.mark_shadow_position("hunter-v1", mint, exit_price)
    book.close_shadow_position("hunter-v1", mint)


def test_one_loss_does_not_block():
    assert reentry_block_reason([SellRecord(-0.5, 1.0)], 0.5) is None


def test_two_losses_block_until_the_first_entry_is_reclaimed():
    sells = [SellRecord(-0.4, 1.0), SellRecord(-0.3, 0.8)]
    reason = reentry_block_reason(sells, 0.77)   # SDOG-style: -23% vs first entry
    assert reason is not None and "reclaims 1" in reason and "-23.0%" in reason
    assert reentry_block_reason(sells, 1.0) is None   # back to 0%: graduated
    assert reentry_block_reason(sells, 1.3) is None


def test_a_win_resets_the_streak():
    sells = [SellRecord(-0.4, 1.0), SellRecord(-0.4, 1.0), SellRecord(0.2, 0.9),
             SellRecord(-0.1, 0.95)]
    assert reentry_block_reason(sells, 0.1) is None


def test_unknown_entry_price_keeps_blocking():
    reason = reentry_block_reason([SellRecord(-1, None), SellRecord(-1, None)], 9.9)
    assert reason is not None and "no entry price" in reason


def test_shadow_book_records_entry_prices_per_sell(tmp_path):
    book = _book(tmp_path)
    _round_trip(book, MINT, 1.0, 0.9)
    _round_trip(book, MINT, 0.8, 0.7)
    history = book.sell_history("hunter-v1", MINT, price_currency="USD")
    assert [s.entry_price for s in history] == [1.0, 0.8]
    assert all(s.realized_usd < 0 for s in history)
    # A different quote currency cannot be compared.
    assert book.sell_history("hunter-v1", MINT, price_currency="SOL")[0].entry_price is None


def test_legacy_fills_without_prices_expire_after_a_day(tmp_path):
    book = _book(tmp_path)
    _round_trip(book, MINT, 1.0, 0.9)
    _round_trip(book, MINT, 1.0, 0.9)
    payload = book.load()
    for fill in payload["agents"]["hunter-v1"]["completed_trades"]:
        fill.pop("entry_price")
    book._write(payload)
    assert len(book.sell_history("hunter-v1", MINT)) == 2
    later = datetime.now(UTC) + timedelta(hours=25)
    assert book.sell_history("hunter-v1", MINT, now=later) == []


def _cycle(tmp_path, monkeypatch, book):
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"generated_at": time.time(),
                                    "candidates": [candidate()]}))  # price 0.9 USD
    monkeypatch.setenv("RECOMMENDATION_SNAPSHOT_PATH", str(snapshot))
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("AGENT_DECISION_LOG_PATH", str(tmp_path / "log.jsonl"))

    class Model:
        def propose(self, *, role, context):
            return {"action": "BUY", "mint": READY_MINT, "requested_usd": 5,
                    "confidence": 0.9, "thesis": "test"}

    return shadow_once(Model(), book, core_only=True)["agents"][0]["arbitration"]


@pytest.mark.parametrize("first_entry, approved", [(1.2, False), (0.85, True)])
def test_shadow_cycle_applies_the_graduation_rule(tmp_path, monkeypatch,
                                                  first_entry, approved):
    book = _book(tmp_path)
    _round_trip(book, READY_MINT, first_entry, first_entry * 0.95)
    _round_trip(book, READY_MINT, 0.8, 0.75)
    arbitration = _cycle(tmp_path, monkeypatch, book)
    assert arbitration["approved"] is approved, arbitration
    if not approved:
        assert any("re-entry block" in r for r in arbitration["reasons"])
