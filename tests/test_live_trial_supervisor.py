"""Pure trial gates: importing this module cannot broadcast."""
import asyncio
from types import SimpleNamespace

import pytest

from solana_launch_guard.live_trial import (
    EMERGENCY_BLOCK_STREAK,
    EMERGENCY_STOP_LOSS_PCT,
    EXIT_STUCK_ALERT_STREAK,
    EXIT_WARNING_MODEL_SKIP_STREAK,
    BoundedTrialModel,
    _eligible_exit,
    _exit_warning_model_call_is_redundant,
    _guarded_entry,
    _guarded_exit,
    _sell_choice,
    _should_escalate_to_emergency,
    _track_exit_block_streak,
    require_exclusive_trial_flags,
)
from solana_launch_guard.live_trial_ledger import LiveTrialLedger, TrialHalted
from solana_launch_guard.live_trial_runner import LiveEntryDecision
from solana_launch_guard.execution import USDC_MINT
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


def test_emergency_escalation_requires_a_hard_stop_loss_plus_a_prior_block():
    """A stop-loss breach alone (first attempt, no prior block) stays on the
    normal path - the immediate fresh-quote retry gets a chance first."""
    assert not _should_escalate_to_emergency(pnl_pct=-25, block_streak=0)
    assert _should_escalate_to_emergency(pnl_pct=-EMERGENCY_STOP_LOSS_PCT - 1, block_streak=1)


def test_emergency_escalation_does_not_trigger_for_a_profitable_position():
    """A winning position waiting for a better fill has little to lose by
    waiting, so weaker triggers (block streak alone) must not escalate it -
    only a real stop-loss breach or a liquidity collapse can."""
    assert not _should_escalate_to_emergency(pnl_pct=40, block_streak=10)
    assert not _should_escalate_to_emergency(pnl_pct=None, block_streak=10)


def test_emergency_escalation_triggers_on_repeated_blocks_while_underwater():
    assert not _should_escalate_to_emergency(pnl_pct=-1, block_streak=EMERGENCY_BLOCK_STREAK - 1)
    assert _should_escalate_to_emergency(pnl_pct=-1, block_streak=EMERGENCY_BLOCK_STREAK)
    assert _should_escalate_to_emergency(pnl_pct=0, block_streak=EMERGENCY_BLOCK_STREAK)


def test_emergency_escalation_liquidity_collapse_overrides_everything():
    """A genuine liquidity collapse (hunter-v1's own deterministic
    assess_exit signal) forces emergency handling even for a profitable,
    never-blocked position - it threatens a winning position too."""
    assert _should_escalate_to_emergency(pnl_pct=50, block_streak=0, liquidity_collapse=True)


def test_exit_warning_model_call_is_redundant_only_after_enough_repeated_blocks():
    """A wallet-scanned position often has no recorded cost basis (pnl_pct
    is None), so it can never reach _should_escalate_to_emergency no matter
    how long it stays stuck - without this separate check it would ask the
    model the same already-answered question forever."""
    assert not _exit_warning_model_call_is_redundant("EXIT WARNING", EXIT_WARNING_MODEL_SKIP_STREAK - 1)
    assert _exit_warning_model_call_is_redundant("EXIT WARNING", EXIT_WARNING_MODEL_SKIP_STREAK)


def test_exit_warning_model_call_is_redundant_only_for_a_full_exit_signal():
    """TAKE_PARTIAL/PROTECT PROFIT sizing genuinely depends on model
    judgment, even after many blocked attempts, so only the unambiguous
    full-liquidation EXIT WARNING signal ever skips the model call."""
    assert not _exit_warning_model_call_is_redundant("TAKE PARTIAL", EXIT_WARNING_MODEL_SKIP_STREAK + 10)
    assert not _exit_warning_model_call_is_redundant("PROTECT PROFIT", EXIT_WARNING_MODEL_SKIP_STREAK + 10)


def test_full_liquidation_bypasses_the_two_dollar_floor_but_partial_sells_do_not():
    """A position that crashed below the usual $2 minimum trade size still
    needs to be fully sellable, or it gets stuck forever - but a
    discretionary partial sell has no such justification, so it keeps the
    $2 floor unchanged.
    """
    crashed = {"action": "SELL", "confidence": .8, "mint": "mint", "requested_usd": 0.12}
    assert _sell_choice(crashed, "mint", 0.12, "EXIT_WARNING") == ("SELL", 1.0)
    # Below even the small dust guard: still rejected.
    assert _sell_choice({**crashed, "requested_usd": 0.001}, "mint", 0.001, "EXIT_WARNING") is None
    # A partial sell of a crashed position stays floored at $2.
    tiny_partial = {"action": "TAKE_PARTIAL", "confidence": .8, "mint": "mint", "requested_usd": 0.5}
    assert _sell_choice(tiny_partial, "mint", 1.0, "TAKE_PARTIAL") is None


def test_eligible_exit_tolerates_a_relabel_between_still_sell_worthy_decisions():
    """The portfolio monitor recomputes this label independently every ~15s;
    for a small, volatile position it can relabel between EXIT WARNING and
    TAKE PARTIAL/PROTECT PROFIT moments later without the underlying
    sell-worthy situation actually changing. That relabel must not block
    the exit - only a move to a non-sell-worthy decision should.
    """
    import time

    def snapshot(decision: str) -> dict:
        return {
            "generated_at": time.time(),
            "signals": [
                {
                    "chain": "solana",
                    "token_address": "mint",
                    "decision": decision,
                    "current_price": 1.5,
                }
            ],
        }

    assert _eligible_exit(snapshot("EXIT WARNING"), "mint") is True
    assert _eligible_exit(snapshot("TAKE PARTIAL"), "mint") is True
    assert _eligible_exit(snapshot("PROTECT PROFIT"), "mint") is True
    assert _eligible_exit(snapshot("HOLD"), "mint") is False
    assert _eligible_exit(snapshot("REBOUND WATCH"), "mint") is False


def test_eligible_exit_reads_raw_decision_for_a_position_crashed_below_sell_floor():
    """PortfolioAdvisor forces `decision` to HOLD once a position's dollar
    value drops below the sell floor, for notification purposes only - but
    `raw_decision` still carries the true risk-based call, and live-trial
    exit eligibility must key off that instead, or a crashed position can
    never be liquidated again.
    """
    import time

    def snapshot(decision: str, raw_decision: str | None) -> dict:
        row = {
            "chain": "solana",
            "token_address": "mint",
            "decision": decision,
            "current_price": 0.001,
        }
        if raw_decision is not None:
            row["raw_decision"] = raw_decision
        return {"generated_at": time.time(), "signals": [row]}

    assert _eligible_exit(snapshot("HOLD", "EXIT WARNING"), "mint") is True
    assert _eligible_exit(snapshot("HOLD", "HOLD"), "mint") is False
    # Older snapshot predating the field: falls back to `decision`.
    assert _eligible_exit(snapshot("EXIT WARNING", None), "mint") is True
    assert _eligible_exit(snapshot("HOLD", None), "mint") is False


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


def test_stale_quote_at_arbitration_blocks_one_sell_without_stopping_trial(tmp_path, monkeypatch):
    """A rejection from the live arbiter's own deterministic checks (e.g. the
    quote aged past the bound during the model round trip) must skip just
    this mint and let the trial keep running, not halt the whole session -
    the same market condition can clear on a later cycle."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    seller = SimpleNamespace(max_price_impact_pct=3, max_slippage_bps=300)
    result = asyncio.run(_guarded_exit(
        ledger=book, agent="portfolio-v1", mint="A" * 44,
        requested_usd=12, current_exit_allowed=lambda: True,
        decision="SELL", fraction=1, position_value_usd=12,
        quote_age_seconds=999, liquidity_usd=60_000,
        rpc=None, seller=seller, store=None, wallet="synthetic-owner",
        symbol="TEST",
    ))
    assert result is None
    assert book.status()["status"] == "ACTIVE"
    assert not book.unresolved()
    decisions = book.status()["recent_decisions"]
    assert decisions[0]["state"] == "EXIT_BLOCKED"
    assert "EXIT BLOCKED" in decisions[0]["reason"]
    assert decisions[1]["state"] == "EXIT_BLOCKED"
    assert "market quote is stale" in decisions[1]["reason"]
    book.close()


def test_exit_stuck_alert_fires_once_after_consecutive_blocks(monkeypatch):
    notified = []

    async def fake_notify(settings, *, title, message):
        notified.append((title, message))

    monkeypatch.setattr("solana_launch_guard.live_trial._notify", fake_notify)
    streaks: dict[str, int] = {}
    mint = "A" * 44

    for _ in range(EXIT_STUCK_ALERT_STREAK - 1):
        asyncio.run(_track_exit_block_streak(
            None, streaks, owner="portfolio-v1", mint=mint, blocked=True,
        ))
    assert notified == []
    assert streaks[mint] == EXIT_STUCK_ALERT_STREAK - 1

    asyncio.run(_track_exit_block_streak(
        None, streaks, owner="portfolio-v1", mint=mint, blocked=True,
    ))
    assert len(notified) == 1
    assert notified[0][0] == "Launch Guard EXIT STUCK"
    assert mint in notified[0][1]

    # Further consecutive blocks don't spam another alert.
    asyncio.run(_track_exit_block_streak(
        None, streaks, owner="portfolio-v1", mint=mint, blocked=True,
    ))
    assert len(notified) == 1

    # A successful exit resets the streak for that mint.
    asyncio.run(_track_exit_block_streak(
        None, streaks, owner="portfolio-v1", mint=mint, blocked=False,
    ))
    assert mint not in streaks


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


def test_price_impact_rejection_blocks_one_buy_without_stopping_trial(tmp_path, monkeypatch):
    """A pre-reservation ValueError (e.g. Jupiter's real quote showing price
    impact over the guarded limit, common on a fast-moving candidate) must
    skip just this attempt and retry next cycle, correctly attributed to
    hunter-v1 - not propagate to supervise()'s generic handler, which can't
    tell a buy failure from a sell failure and always mislabels it
    agent="portfolio-v1"."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    decision = LiveEntryDecision(
        agent="hunter-v1", mint="A" * 44, requested_cents=500,
        approved_cents=500, reason="test", candidate={},
    )

    class Rpc:
        async def token_balance(self, owner, mint):
            if mint == USDC_MINT:
                return SimpleNamespace(raw_amount=20_000_000)
            return SimpleNamespace(raw_amount=0)
        async def mint_decimals(self, mint):
            return 6

    class Buyer:
        max_price_impact_pct = 3
        max_slippage_bps = 300
        async def preflight(self, intent, rpc):
            raise ValueError("Jupiter price impact -4.49% exceeds the 3.00% limit (exact quote -4.491513%)")

    result = asyncio.run(_guarded_entry(
        decision, ledger=book, rpc=Rpc(), buyer=Buyer(), store=None,
        wallet="synthetic-owner", current_snapshot=lambda: {},
    ))
    assert result is None
    assert book.status()["status"] == "ACTIVE"
    assert not book.unresolved()
    decisions = book.status()["recent_decisions"]
    assert decisions[0]["agent"] == "hunter-v1"
    assert decisions[0]["state"] == "BUY_BLOCKED"
    assert "price impact" in decisions[0]["reason"]
    book.close()


def test_rejected_buy_never_suppresses_an_unresolved_order(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    book.reserve_buy(intent="already-submitted", agent="hunter-v1", mint="A" * 44,
                     requested_cents=500, approved_cents=500)
    decision = LiveEntryDecision(
        agent="hunter-v1", mint="A" * 44, requested_cents=500,
        approved_cents=500, reason="test", candidate={},
    )

    class Rpc:
        async def token_balance(self, owner, mint):
            if mint == USDC_MINT:
                return SimpleNamespace(raw_amount=20_000_000)
            return SimpleNamespace(raw_amount=0)
        async def mint_decimals(self, mint):
            return 6

    class Buyer:
        max_price_impact_pct = 3
        max_slippage_bps = 300
        async def preflight(self, intent, rpc):
            raise ValueError("Jupiter price impact exceeds the limit")

    with pytest.raises(ValueError, match="price impact"):
        asyncio.run(_guarded_entry(
            decision, ledger=book, rpc=Rpc(), buyer=Buyer(), store=None,
            wallet="synthetic-owner", current_snapshot=lambda: {},
        ))
    assert len(book.unresolved()) == 1
    book.close()
