"""Pure trial gates: importing this module cannot broadcast."""
import asyncio
import pytest

from solana_launch_guard.live_trial import BoundedTrialModel, _guarded_exit, _sell_choice, require_exclusive_trial_flags
from solana_launch_guard.live_trial_ledger import LiveTrialLedger, TrialHalted
from solana_launch_guard.agents import AgentRole


def test_live_start_flags_block_parallel_auto_execution(monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_TEST_ENABLED", "true")
    monkeypatch.setenv("AGENT_LIVE_TRIAL_ENABLED", "true")
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    monkeypatch.setenv("AGENT_LIVE_CANARY_ONLY", "false")
    for name in ("AUTO_BUY_LIVE", "AUTO_SELL_LIVE", "AUTO_REBUY_ENABLED"):
        monkeypatch.setenv(name, "false")
    monkeypatch.setenv("AGENT_LIVE_CANARY_ONLY", "true")
    with pytest.raises(TrialHalted, match="CANARY_ONLY"):
        require_exclusive_trial_flags()
    monkeypatch.setenv("AGENT_LIVE_CANARY_ONLY", "false")
    require_exclusive_trial_flags()
    for name in ("AUTO_BUY_LIVE", "AUTO_SELL_LIVE", "AUTO_REBUY_ENABLED"):
        monkeypatch.setenv(name, "true")
        with pytest.raises(TrialHalted, match="exclusive"):
            require_exclusive_trial_flags()
        monkeypatch.setenv(name, "false")


def test_exit_choice_requires_matched_signal_mint_confidence_and_amount():
    value = {"action": "SELL", "confidence": .8, "mint": "mint", "requested_usd": 12}
    assert _sell_choice(value, "mint", 12, "EXIT_WARNING") == ("SELL", 1.0)
    assert _sell_choice(value, "another", 12, "EXIT_WARNING") is None
    assert _sell_choice(value, "mint", 12, "HOLD") is None
    assert _sell_choice({**value, "requested_usd": 5}, "mint", 12, "EXIT WARNING") is None
    assert _sell_choice({**value, "confidence": .2}, "mint", 12, "EXIT_WARNING") is None
    partial = {**value, "action": "TAKE_PARTIAL", "requested_usd": 4}
    assert _sell_choice(partial, "mint", 12, "TAKE_PARTIAL") == ("TAKE_PARTIAL", 1 / 3)
    assert _sell_choice(partial, "mint", 12, "TAKE_PARTIAL", .25) is None
    assert _sell_choice(partial, "mint", 12, "TAKE_PARTIAL", .5) == ("TAKE_PARTIAL", 1 / 3)


def test_editing_env_kill_switch_stops_existing_process(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    (tmp_path / ".env").write_text("AGENT_LIVE_KILL_SWITCH=false\n")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start(now=100)
    (tmp_path / ".env").write_text("AGENT_LIVE_KILL_SWITCH=true\n")
    with pytest.raises(TrialHalted, match=".env"):
        book.reserve_buy(intent="buy", agent="hunter-v1", mint="mint",
                         requested_cents=500, approved_cents=500, now=101)
    book.close()


def test_model_request_budget_persists_across_reopen(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    monkeypatch.setattr("solana_launch_guard.live_trial.MAX_MODEL_REQUESTS", 1)
    path = tmp_path / "trial.sqlite"
    book = LiveTrialLedger(path)
    book.start()
    class Model:
        def propose(self, **kwargs):
            return {"action": "HOLD"}
    assert BoundedTrialModel(Model(), book).propose(role=AgentRole.PORTFOLIO_MANAGER, context={})["action"] == "HOLD"
    book.close()
    book = LiveTrialLedger(path)
    with pytest.raises(TrialHalted, match="model requests"):
        BoundedTrialModel(Model(), book).propose(role=AgentRole.PORTFOLIO_MANAGER, context={})
    assert book.model_request_count() == 1
    book.close()


def test_expired_exit_blocks_one_sell_without_stopping_trial(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    result = asyncio.run(_guarded_exit(
        ledger=book, agent="portfolio-v1", mint="A" * 44,
        requested_usd=12, current_exit_allowed=lambda: False,
        decision="SELL", fraction=1, position_value_usd=12,
        quote_age_seconds=2, liquidity_usd=60_000,
        rpc=None, seller=None, store=None, wallet="synthetic-owner",
        symbol="TEST",
    ))
    assert result is None
    assert book.status()["status"] == "ACTIVE"
    assert not book.unresolved()
    assert book.status()["recent_decisions"][0]["state"] == "EXIT_BLOCKED"
    book.close()


def test_expired_exit_never_suppresses_an_unresolved_order(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    book.reserve_sell(intent="already-submitted", agent="portfolio-v1", mint="A" * 44)
    with pytest.raises(TrialHalted, match="EXIT BLOCKED"):
        asyncio.run(_guarded_exit(
            ledger=book, agent="portfolio-v1", mint="A" * 44,
            requested_usd=12, current_exit_allowed=lambda: False,
            decision="SELL", fraction=1, position_value_usd=12,
            quote_age_seconds=2, liquidity_usd=60_000,
            rpc=None, seller=None, store=None, wallet="synthetic-owner",
            symbol="TEST",
        ))
    assert len(book.unresolved()) == 1
    book.close()
