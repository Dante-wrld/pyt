from datetime import UTC, date, datetime, timedelta

import pytest

from solana_launch_guard.agents import (
    AgentRecord,
    AgentRole,
    AgentStatus,
    LedgerEntry,
    RiskArbiter,
    RiskSnapshot,
    SurvivalEvaluator,
    TradeAction,
    TradeProposal,
    calculate_performance,
    daily_report,
)
from solana_launch_guard.learning import (
    LearningPolicy,
    LearningSource,
    PromotionEvidence,
    StrategyLesson,
)


def proposal(**overrides):
    values = dict(
        agent_id="hunter-v1",
        role=AgentRole.OPPORTUNITY_HUNTER,
        action=TradeAction.BUY,
        mint="mint",
        requested_usd=20,
        confidence=0.8,
        thesis="bounded paper candidate",
    )
    values.update(overrides)
    return TradeProposal(**values)


def risk(**overrides):
    values = dict(equity_usd=500, liquidity_usd=20_000)
    values.update(overrides)
    return RiskSnapshot(**values)


def test_arbiter_caps_agent_request_to_five_dollars():
    result = RiskArbiter().evaluate(proposal(), risk())
    assert result.approved
    assert result.approved_usd == 5


@pytest.mark.parametrize(
    "override, reason",
    [
        ({"mode": "live"}, "mode"),
        ({"kill_switch": True}, "kill switch"),
        ({"liquidity_usd": 100}, "liquidity"),
        ({"quoted_price_impact_pct": -6}, "price impact"),
        ({"quote_age_seconds": 16}, "stale"),
        ({"open_positions": 2}, "positions"),
        ({"daily_realized_pnl_usd": -16}, "daily loss"),
    ],
)
def test_arbiter_rejects_unsafe_state(override, reason):
    result = RiskArbiter().evaluate(proposal(), risk(**override))
    assert not result.approved
    assert any(reason in item for item in result.reasons)


def test_copy_buy_requires_leader_attribution():
    with pytest.raises(ValueError, match="leader wallet"):
        proposal(role=AgentRole.COPY_TRADER).validate()


def test_agent_roles_cannot_cross_mandates():
    with pytest.raises(ValueError, match="cannot originate buys"):
        proposal(role=AgentRole.PORTFOLIO_MANAGER).validate()
    with pytest.raises(ValueError, match="cannot manage owned positions"):
        proposal(action=TradeAction.SELL).validate()


def test_portfolio_rebuy_is_zero_dollar_advisory():
    review = proposal(role=AgentRole.PORTFOLIO_MANAGER,
                      action=TradeAction.REBUY, requested_usd=0)
    review.validate()
    result = RiskArbiter().evaluate(review, risk())
    assert not result.approved and result.approved_usd == 0
    with pytest.raises(ValueError, match="zero-dollar"):
        proposal(role=AgentRole.PORTFOLIO_MANAGER,
                 action=TradeAction.REBUY, requested_usd=5).validate()


def test_hunter_can_review_a_rebuy_without_spending():
    review = proposal(action=TradeAction.REBUY, requested_usd=0)
    review.validate()
    assert not RiskArbiter().evaluate(review, risk()).approved


def test_entry_only_limits_do_not_trap_risk_reducing_sell():
    result = RiskArbiter().evaluate(
        proposal(
            role=AgentRole.PORTFOLIO_MANAGER,
            action=TradeAction.SELL,
            requested_usd=5,
        ),
        risk(liquidity_usd=100, open_positions=2, daily_realized_pnl_usd=-16),
    )
    assert result.approved


def test_daily_report_keeps_agent_attribution_and_net_fees():
    when = datetime(2026, 9, 19, tzinfo=UTC)
    rows = [
        LedgerEntry("a", AgentRole.OPPORTUNITY_HUNTER, "x", 4, 1, 10, when),
        LedgerEntry("b", AgentRole.COPY_TRADER, "y", -2, 0.5, 10, when),
    ]
    report = daily_report(rows, date(2026, 9, 19))
    assert report["total"]["net_pnl_usd"] == 0.5
    assert set(report["agents"]) == {"a", "b"}


def test_survival_requires_time_and_sample_before_retirement():
    record = AgentRecord(
        "copy-v1",
        AgentRole.COPY_TRADER,
        born_at=datetime.now(UTC) - timedelta(days=2),
    )
    bad = calculate_performance(
        [LedgerEntry("copy-v1", record.role, str(i), -1, capital_deployed_usd=5) for i in range(40)]
    )
    assert SurvivalEvaluator().evaluate(record, bad).status is AgentStatus.INCUBATING


def test_video_lesson_is_attributed_and_cannot_skip_research_status():
    transcript = "A testable strategy needs entry, exit, invalidation, and risk rules. " * 3
    source = LearningSource.from_transcript(
        title="Risk lesson", author="Professional", url="https://example.com/video", transcript=transcript
    )
    excerpt = "A testable strategy needs entry, exit, invalidation, and risk rules."
    lesson = StrategyLesson("one", source.source_id, "test rules", excerpt, "enter", "exit", "invalidate", "risk")
    lesson.validate(source)


def test_learning_promotion_requires_forward_evidence_after_costs():
    ok, reasons = LearningPolicy().may_promote(
        PromotionEvidence(50, 3, 8, 30, True)
    )
    assert ok
    assert "passed" in reasons[0]
    denied, _ = LearningPolicy().may_promote(
        PromotionEvidence(10, 30, 1, 2, False)
    )
    assert not denied
def test_live_exit_does_not_apply_new_buy_cap():
    from solana_launch_guard.agents import RiskArbiter, RiskPolicy, RiskSnapshot

    arbiter = RiskArbiter(RiskPolicy(allowed_modes=("live",), max_order_usd=5))
    state = RiskSnapshot(mode="live", current_position_usd=12, quote_age_seconds=2)
    assert arbiter.evaluate_live_exit(12, state).approved_usd == 12
    assert not arbiter.evaluate_live_exit(13, state).approved
    assert not arbiter.evaluate_live_exit(1.99, state).approved
    assert not arbiter.evaluate_live_exit(12, RiskSnapshot(mode="live", current_position_usd=12, kill_switch=True)).approved
