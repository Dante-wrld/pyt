from __future__ import annotations

import time

import pytest

from solana_launch_guard.agent_live_test import (
    CanaryJournal,
    CanaryPolicy,
    LIVE_CONFIRMATION,
    select_live_candidate,
    validate_live_environment,
    validate_exit_quote,
    canary_exit_allowed,
)


def candidate_snapshot(**overrides):
    candidate = {
        "chain": "solana",
        "mint": "A" * 32,
        "decision": "BUY NOW",
        "quoted_at": time.time(),
        "liquidity_usd": 75_000,
        "entry_confirmation_count": 3,
        "entry_confirmation_required": 3,
    }
    candidate.update(overrides)
    return {"generated_at": time.time(), "candidates": [candidate]}


def test_selects_only_confirmed_liquid_fresh_candidate():
    selected = select_live_candidate(candidate_snapshot(), CanaryPolicy())
    assert selected["mint"] == "A" * 32
    with pytest.raises(ValueError, match="no fresh"):
        select_live_candidate(candidate_snapshot(liquidity_usd=49_999), CanaryPolicy())
    with pytest.raises(ValueError, match="no fresh"):
        select_live_candidate(
            candidate_snapshot(entry_confirmation_count=2), CanaryPolicy()
        )


def test_stale_snapshot_is_rejected():
    snapshot = candidate_snapshot()
    snapshot["generated_at"] = time.time() - 16
    with pytest.raises(ValueError, match="stale"):
        select_live_candidate(snapshot, CanaryPolicy())


def test_fresh_snapshot_cannot_republish_stale_or_missing_quote():
    policy = CanaryPolicy()
    with pytest.raises(ValueError, match="no fresh"):
        select_live_candidate(candidate_snapshot(quoted_at=time.time() - 16), policy)
    with pytest.raises(ValueError, match="no fresh"):
        select_live_candidate(candidate_snapshot(quoted_at=None), policy)
    with pytest.raises(ValueError, match="no fresh"):
        select_live_candidate(candidate_snapshot(quoted_at=time.time() + 60), policy)


def test_live_environment_is_default_off(monkeypatch):
    monkeypatch.delenv("AGENT_LIVE_TEST_ENABLED", raising=False)
    with pytest.raises(ValueError, match="ENABLED is false"):
        validate_live_environment(execute=False, confirmation=None)


def test_execution_needs_kill_switch_off_exit_and_literal(monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_TEST_ENABLED", "true")
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    monkeypatch.setenv("SOLANA_WALLET_ADDRESS", "A" * 32)
    monkeypatch.setenv("JUPITER_API_KEY", "test")
    monkeypatch.setenv("AUTO_SELL_ENABLED", "true")
    monkeypatch.setenv("AUTO_SELL_LIVE", "true")
    monkeypatch.setenv("AUTO_SELL_PORTFOLIO_SIGNALS", "true")
    monkeypatch.setenv("AGENT_LIVE_CANARY_ONLY", "true")
    monkeypatch.setenv("AUTO_BUY_LIVE", "false")
    monkeypatch.setenv("AUTO_REBUY_ENABLED", "false")
    with pytest.raises(ValueError, match="requires --confirm"):
        validate_live_environment(execute=True, confirmation="wrong")
    validate_live_environment(execute=True, confirmation=LIVE_CONFIRMATION)


def test_canary_journal_is_one_attempt_only(tmp_path):
    journal = CanaryJournal(tmp_path / "canary.json")
    journal.assert_unused()
    journal.record({"status": "PENDING"})
    with pytest.raises(ValueError, match="already been attempted"):
        journal.assert_unused()


def test_canary_claim_survives_restart(tmp_path):
    path = tmp_path / "canary.json"
    CanaryJournal(path).claim({"status": "PENDING"})
    with pytest.raises(ValueError, match="already"):
        CanaryJournal(path).claim({"status": "PENDING"})


def test_canary_requires_exact_five_dollars(monkeypatch):
    for amount in ("1", "6", "nan", "inf"):
        monkeypatch.setenv("AGENT_LIVE_TEST_AMOUNT_USD", amount)
        with pytest.raises(ValueError, match="exactly"):
            CanaryPolicy.from_env()
    monkeypatch.setenv("AGENT_LIVE_TEST_AMOUNT_USD", "5")
    assert CanaryPolicy.from_env().amount_usd == 5


def test_canary_exit_is_scoped_to_confirmed_test_token(tmp_path, monkeypatch):
    path = tmp_path / "canary.json"
    monkeypatch.setenv("AGENT_LIVE_CANARY_PATH", str(path))
    assert not canary_exit_allowed("test")
    book = CanaryJournal(path)
    book.record({"mint": "test", "status": "PENDING"})
    assert not canary_exit_allowed("test")
    book.record({"mint": "test", "status": "CONFIRMED"})
    assert canary_exit_allowed("test")
    assert not canary_exit_allowed("other")


def test_exit_quote_must_cover_sell_floor(monkeypatch):
    monkeypatch.setenv("AUTO_SELL_MIN_VALUE_USD", "2")
    monkeypatch.setenv("PORTFOLIO_MIN_SELL_VALUE_USD", "2")
    quote = {"inputMint": "token", "outputMint": "usdc", "inAmount": "100",
             "outAmount": "4900000", "otherAmountThreshold": "4800000",
             "priceImpact": 1, "slippageBps": 250}
    args = dict(mint="token", amount_raw=100, usdc_mint="usdc", policy=CanaryPolicy())
    assert validate_exit_quote(quote, **args) == 4.8
    for override in ({"otherAmountThreshold": "1900000"}, {"inputMint": "wrong"},
                     {"priceImpact": 4}, {"inAmount": "99"}):
        with pytest.raises(ValueError):
            validate_exit_quote({**quote, **override}, **args)
