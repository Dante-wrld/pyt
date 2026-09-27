"""The hunter-v1 shadow loop applies the live trial's entry gates, so paper
results show what live hunter-v1 would have bought (2026-09-26)."""
import json
import time

import pytest
from solana_launch_guard.agent_capital import CapitalBook
from solana_launch_guard.agent_cli import shadow_once

from test_hunter_shadow_strategy import MINT, candidate

DAY_MS = 86_400_000


def _run(tmp_path, monkeypatch, allowed="BUY ZONE", **row):
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"generated_at": time.time(),
                                    "candidates": [candidate(**row)]}))
    monkeypatch.setenv("RECOMMENDATION_SNAPSHOT_PATH", str(snapshot))
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("AGENT_DECISION_LOG_PATH", str(tmp_path / "log.jsonl"))
    # The frozen live profile.
    monkeypatch.setenv("ENTRY_ALLOWED_DECISIONS", allowed)
    monkeypatch.setenv("ENTRY_MIN_TOKEN_AGE_DAYS", "3")
    book = CapitalBook(tmp_path / "capital.json")
    book.initialize(30)

    class Model:
        def propose(self, *, role, context):
            return {"action": "BUY", "mint": MINT, "requested_usd": 5,
                    "confidence": 0.9, "thesis": "test"}

    result = shadow_once(Model(), book, core_only=True)
    return result, book


def _old():
    return time.time() * 1000 - 10 * DAY_MS


def test_buy_zone_on_an_old_token_still_buys(tmp_path, monkeypatch):
    result, book = _run(tmp_path, monkeypatch, pair_created_at_ms=_old())
    assert result["hunter_candidate_reviews"][MINT]["state"] == "BUY_READY"
    assert result["agents"][0]["arbitration"]["approved"]
    assert MINT in book.load()["agents"]["hunter-v1"]["positions"]


@pytest.mark.parametrize("row, reason", [
    ({"decision": "BUY NOW"}, "not in ENTRY_ALLOWED_DECISIONS"),
    ({"decision": "EARLY BUY"}, "not in ENTRY_ALLOWED_DECISIONS"),
    ({"pair_created_at_ms": time.time() * 1000 - DAY_MS}, "ENTRY_MIN_TOKEN_AGE_DAYS"),
    ({"pair_created_at_ms": None}, "token age is unknown"),
    ({"sources": ["leader-held"]}, "ENTRY_SHADOW_ONLY_SOURCES"),
])
def test_signals_live_would_skip_are_not_bought(tmp_path, monkeypatch, row, reason):
    row = {"pair_created_at_ms": _old(), **row}
    result, book = _run(tmp_path, monkeypatch, **row)
    review = result["hunter_candidate_reviews"][MINT]
    agent = result["agents"][0]
    assert review["state"] == "PAUSED"
    assert any(reason in r for r in review["reasons"])
    assert not agent["arbitration"]["approved"]
    assert book.load()["agents"]["hunter-v1"]["positions"] == {}


def _momentum_row():
    # The pause applies whatever assess_entry concluded.
    return {"decision": "MOMENTUM BUY", "pair_created_at_ms": _old()}


def test_momentum_buy_is_paused_like_live(tmp_path, monkeypatch):
    # Even when the profile allows it, MOMENTUM_BUY_PAUSED holds it back.
    result, book = _run(tmp_path, monkeypatch, allowed="BUY ZONE,MOMENTUM BUY",
                        **_momentum_row())
    review = result["hunter_candidate_reviews"][MINT]
    assert review["state"] == "PAUSED"
    assert "MOMENTUM BUY is paused" in review["reasons"][0]
    assert book.load()["agents"]["hunter-v1"]["positions"] == {}
    assert not result["agents"][0]["arbitration"]["approved"]


def _lose_today(book, usd):
    """Record a closed losing trade of `usd` today."""
    book.reserve_shadow_buy(agent_id="hunter-v1", mint="L" * 44, symbol="L",
                            amount_usd=5, entry_price=1.0, price_currency="USD")
    book.mark_shadow_position("hunter-v1", "L" * 44, 1 - usd / 5)
    book.close_shadow_position("hunter-v1", "L" * 44)


@pytest.mark.parametrize("loss, approved", [(1.15, True), (3.10, False)])
def test_daily_loss_limit_matches_live(tmp_path, monkeypatch, loss, approved):
    """2026-09-27: -$1.15 in the first UTC hour tripped the shadow's generic
    3%-of-equity limit (~$0.84) and froze it for the day; live allows 10% of
    $30 = $3.00."""
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"generated_at": time.time(), "candidates": [
        candidate(pair_created_at_ms=_old())]}))
    monkeypatch.setenv("RECOMMENDATION_SNAPSHOT_PATH", str(snapshot))
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("AGENT_DECISION_LOG_PATH", str(tmp_path / "log.jsonl"))
    book = CapitalBook(tmp_path / "capital.json")
    book.initialize(30)
    _lose_today(book, loss)

    class Model:
        def propose(self, *, role, context):
            return {"action": "BUY", "mint": MINT, "requested_usd": 5,
                    "confidence": 0.9, "thesis": "test"}

    arbitration = shadow_once(Model(), book, core_only=True)["agents"][0]["arbitration"]
    assert arbitration["approved"] is approved, arbitration
    if not approved:
        assert "daily loss limit reached" in arbitration["reasons"]
