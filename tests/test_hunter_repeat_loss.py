"""hunter-v1 shadow re-bought SDOG and 98kfF7 four times each, losing every
time (2026-09-26). It now carries coin_tracker's repeat-loss block."""
import json
import time
from datetime import UTC, datetime, timedelta

from solana_launch_guard.agent_capital import REPEAT_LOSS_BLOCK_COUNT, CapitalBook
from solana_launch_guard.agent_cli import shadow_once

from test_hunter_shadow_strategy import MINT as READY_MINT
from test_hunter_shadow_strategy import candidate

MINT = "S" * 44
OTHER = "O" * 44


def _book(tmp_path):
    book = CapitalBook(tmp_path / "capital.json")
    book.initialize(30)
    return book


def _round_trip(book, mint, exit_price, *, principal_first=False):
    book.reserve_shadow_buy(agent_id="hunter-v1", mint=mint, symbol="S", amount_usd=5,
                            entry_price=1.0, price_currency="USD")
    if principal_first:
        book.mark_shadow_position("hunter-v1", mint, 2.0)
        book.close_shadow_position("hunter-v1", mint, fraction=0.5,
                                   stage="PRINCIPAL_RECOVERY")
    book.mark_shadow_position("hunter-v1", mint, exit_price)
    book.close_shadow_position("hunter-v1", mint)


def test_counts_losing_round_trips_per_mint(tmp_path):
    book = _book(tmp_path)
    _round_trip(book, MINT, 0.95)
    assert book.recent_losing_exits("hunter-v1", MINT) == 1
    _round_trip(book, MINT, 0.90)
    assert book.recent_losing_exits("hunter-v1", MINT) == REPEAT_LOSS_BLOCK_COUNT
    assert book.recent_losing_exits("hunter-v1", OTHER) == 0


def test_breakeven_and_winning_trades_do_not_count(tmp_path):
    book = _book(tmp_path)
    _round_trip(book, MINT, 1.0)
    _round_trip(book, MINT, 1.2)
    assert book.recent_losing_exits("hunter-v1", MINT) == 0


def test_a_trade_is_judged_on_all_its_fills(tmp_path):
    # Principal sold at 2x, remainder exited below entry: net winner overall.
    book = _book(tmp_path)
    _round_trip(book, MINT, 0.9, principal_first=True)
    assert book.recent_losing_exits("hunter-v1", MINT) == 0


def test_open_position_is_not_counted(tmp_path):
    book = _book(tmp_path)
    book.reserve_shadow_buy(agent_id="hunter-v1", mint=MINT, symbol="S", amount_usd=5,
                            entry_price=1.0, price_currency="USD")
    book.mark_shadow_position("hunter-v1", MINT, 2.0)
    book.close_shadow_position("hunter-v1", MINT, fraction=0.5,
                               stage="PRINCIPAL_RECOVERY", slippage_pct=60)
    assert book.recent_losing_exits("hunter-v1", MINT) == 0


def test_losses_age_out_of_the_window(tmp_path):
    book = _book(tmp_path)
    _round_trip(book, MINT, 0.9)
    _round_trip(book, MINT, 0.9)
    later = datetime.now(UTC) + timedelta(hours=25)
    assert book.recent_losing_exits("hunter-v1", MINT, now=later) == 0


def test_uninitialized_book_blocks_nothing(tmp_path):
    book = CapitalBook(tmp_path / "none.json")
    assert book.recent_losing_exits("hunter-v1", MINT) == 0


def test_shadow_cycle_refuses_a_mint_it_keeps_losing_on(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"generated_at": time.time(),
                                    "candidates": [candidate()]}))
    monkeypatch.setenv("RECOMMENDATION_SNAPSHOT_PATH", str(snapshot))
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("AGENT_DECISION_LOG_PATH", str(tmp_path / "log.jsonl"))

    class Model:
        def propose(self, *, role, context):
            return {"action": "BUY", "mint": READY_MINT, "requested_usd": 5,
                    "confidence": 0.9, "thesis": "test"}

    (tmp_path / "fresh").mkdir()
    fresh = _book(tmp_path / "fresh")
    approved = shadow_once(Model(), fresh, core_only=True)["agents"][0]
    assert approved["arbitration"]["approved"], approved["arbitration"]

    book = _book(tmp_path)
    _round_trip(book, READY_MINT, 0.95)
    _round_trip(book, READY_MINT, 0.90)
    blocked = shadow_once(Model(), book, core_only=True)["agents"][0]
    assert not blocked["arbitration"]["approved"]
    assert any("repeat-loss block" in r for r in blocked["arbitration"]["reasons"])
    assert book.load()["agents"]["hunter-v1"]["positions"] == {}
