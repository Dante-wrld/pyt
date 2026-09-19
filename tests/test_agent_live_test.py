from __future__ import annotations

import time

import pytest

from solana_launch_guard.agent_live_test import (
    CanaryJournal,
    CanaryPolicy,
    LIVE_CONFIRMATION,
    select_live_candidate,
    validate_live_environment,
)


def candidate_snapshot(**overrides):
    candidate = {
        "chain": "solana",
        "mint": "A" * 32,
        "decision": "BUY NOW",
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
    with pytest.raises(ValueError, match="requires --confirm"):
        validate_live_environment(execute=True, confirmation="wrong")
    validate_live_environment(execute=True, confirmation=LIVE_CONFIRMATION)


def test_canary_journal_is_one_attempt_only(tmp_path):
    journal = CanaryJournal(tmp_path / "canary.json")
    journal.assert_unused()
    journal.record({"status": "PENDING"})
    with pytest.raises(ValueError, match="already been attempted"):
        journal.assert_unused()
