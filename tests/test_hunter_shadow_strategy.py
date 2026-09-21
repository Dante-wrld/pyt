import json
import time

from solana_launch_guard.agent_capital import CapitalBook
from solana_launch_guard.agents import AgentRole
from solana_launch_guard.agent_cli import funnel_report, shadow_once
from solana_launch_guard.hunter_shadow_strategy import ShadowRecoveryPolicy, assess_entry, assess_exit


MINT = "A" * 32


def candidate(**changes):
    row = {"mint": MINT, "chain": "solana", "symbol": "A", "decision": "BUY ZONE",
           "price": 0.9, "price_currency": "USD", "peak_price": 1.0,
           "pullback_from_peak_pct": 10, "liquidity_usd": 20_000,
           "initial_liquidity_usd": 20_000, "price_change_m5_pct": 3,
           "momentum_label": "RISING", "volume_label": "STEADY",
           "buys_m5": 20, "sells_m5": 10, "buy_sell_ratio": 2,
           "risk_label": "MEDIUM", "signal_score": 80,
           "entry_confirmation_count": 3, "entry_confirmation_required": 3,
           "quoted_at": time.time()}
    row.update(changes)
    return row


def test_falling_pullback_is_never_buy_ready():
    result = assess_entry(candidate(momentum_label="FALLING", price_change_m5_pct=-9), ShadowRecoveryPolicy())
    assert result["state"] == "PULLBACK_STARTED"
    assert "momentum" in " ".join(result["reasons"])


def test_momentum_buy_does_not_require_a_pullback():
    result = assess_entry(
        candidate(decision="MOMENTUM BUY", price=1.0, peak_price=1.0,
                  pullback_from_peak_pct=0, volume_label="RISING"),
        ShadowRecoveryPolicy(),
    )
    assert result["state"] == "BUY_READY"
    assert "pullback" not in result["failure_codes"]


def test_momentum_buy_accepts_steady_volume_not_just_rising():
    result = assess_entry(
        candidate(decision="MOMENTUM BUY", price=1.0, peak_price=1.0,
                  pullback_from_peak_pct=0, volume_label="STEADY"),
        ShadowRecoveryPolicy(),
    )
    assert result["state"] == "BUY_READY"


def test_momentum_buy_still_rejects_falling_volume():
    result = assess_entry(
        candidate(decision="MOMENTUM BUY", price=1.0, peak_price=1.0,
                  pullback_from_peak_pct=0, volume_label="FALLING"),
        ShadowRecoveryPolicy(),
    )
    assert result["state"] != "BUY_READY"
    assert "volume" in result["failure_codes"]


def test_momentum_buy_requires_a_higher_buy_sell_ratio_than_the_pullback_path():
    # 1.3 clears the pullback path's 1.2 bar but not momentum's stricter 1.5.
    result = assess_entry(
        candidate(decision="MOMENTUM BUY", price=1.0, peak_price=1.0,
                  pullback_from_peak_pct=0, volume_label="RISING", buy_sell_ratio=1.3),
        ShadowRecoveryPolicy(),
    )
    assert result["state"] != "BUY_READY"
    assert "buy_sell_ratio" in result["failure_codes"]


def test_zero_confirmations_and_absent_activity_remain_watch():
    result = assess_entry(candidate(entry_confirmation_count=0, volume_label="UNKNOWN"), ShadowRecoveryPolicy())
    assert result["decision"] == "WATCH"
    assert "entry confirmations 0/3" in result["reasons"]


def test_assess_entry_reports_stable_failure_codes_alongside_human_reasons():
    """The human-readable reasons embed live numbers (e.g. "entry
    confirmations 0/3") and can't be aggregated by simple string counting.
    failure_codes gives each failure a fixed tag for exactly that purpose.
    """
    review = assess_entry(
        candidate(risk_label="HIGH", price=0.97, peak_price=1.0,
                  pullback_from_peak_pct=3),
        ShadowRecoveryPolicy(),
    )
    assert set(review["failure_codes"]) == {"pullback", "risk"}


def test_funnel_report_counts_decisions_and_hunter_entry_blockers(
    tmp_path, monkeypatch
):
    """A strategy question like "why isn't the trial buying anything?"
    needs real counts across both the recommendation engine's own decision
    and the stricter, independent policy that actually gates a live buy -
    a candidate can fail the second silently with no trace anywhere else.
    """
    snapshot_path = tmp_path / "recommendations.json"
    snapshot_path.write_text(json.dumps({
        "generated_at": time.time(),
        "tracked_candidates": [
            candidate(),  # passes every check: BUY_READY
            candidate(decision="WAIT FOR PULLBACK", price=0.97, peak_price=1.0,
                      pullback_from_peak_pct=3),
            candidate(decision="PULLBACK STARTED", risk_label="HIGH"),
        ],
    }))
    monkeypatch.setenv("RECOMMENDATION_SNAPSHOT_PATH", str(snapshot_path))

    report = funnel_report()

    assert report["total_candidates"] == 3
    assert report["recommendation_decision_counts"] == {
        "BUY ZONE": 1, "WAIT FOR PULLBACK": 1, "PULLBACK STARTED": 1,
    }
    assert report["hunter_entry_state_counts"] == {
        "BUY_READY": 1, "WATCH": 1, "RECOVERY_CONFIRMING": 1,
    }
    assert report["hunter_entry_blocking_reasons"] == {
        "no_decision": 2, "pullback": 1, "risk": 1,
    }


def test_confirmed_recovery_becomes_buy_ready_but_risk_failure_blocks():
    assert assess_entry(candidate(), ShadowRecoveryPolicy())["state"] == "BUY_READY"
    assert assess_entry(candidate(liquidity_usd=100), ShadowRecoveryPolicy())["decision"] == "WATCH"
    assert assess_entry(candidate(risk_label="HIGH"), ShadowRecoveryPolicy())["decision"] == "WATCH"


def test_one_tick_small_dip_and_profit_running_do_not_exit():
    position = {"entry_price": 1, "highest_price_since_entry": 1.3, "allocated_usd": 5, "current_value_usd": 6.3}
    review = assess_exit(position, candidate(price=1.26, price_change_m5_pct=-1, buys_m5=10, sells_m5=11), ShadowRecoveryPolicy())
    assert review["state"] == "PROFIT_RUNNING"


def test_confirmed_reversal_and_liquidity_collapse_take_priority_over_partial():
    position = {"entry_price": 1, "highest_price_since_entry": 3.5, "entry_liquidity_usd": 20_000}
    row = candidate(price=2.9, price_change_m5_pct=-9, buys_m5=2, sells_m5=12)
    assert assess_exit(position, row, ShadowRecoveryPolicy())["state"] == "EXIT"
    assert assess_exit(position, {**row, "liquidity_usd": 1_000}, ShadowRecoveryPolicy())["state"] == "EMERGENCY_EXIT"
    assert assess_exit(position, candidate(price=3.5), ShadowRecoveryPolicy())["state"] == "TAKE_PARTIAL"


def test_trailing_stop_exits_a_considerable_rise_that_reverses_with_selling_pressure():
    """A token that rose considerably (>=20% from entry) and has already
    pulled back >=12% from its peak exits once corroborated by real selling
    pressure (sells_m5 > buys_m5, populated by both live_trial.py's and
    agent_cli.py's callers even when volume_label is "UNKNOWN") or a falling
    volume label - it must not exit on the pullback alone, which can be a
    single noisy tick on thin liquidity rather than a genuine reversal.
    """
    position = {"entry_price": 1, "highest_price_since_entry": 1.3}
    row = candidate(price=1.1, price_change_m5_pct=-2, buys_m5=8, sells_m5=10,
                     volume_label="UNKNOWN")
    review = assess_exit(position, row, ShadowRecoveryPolicy())
    assert review["state"] == "EXIT"
    assert "trailing" in " ".join(review["reasons"]).lower()


def test_trailing_stop_without_selling_pressure_only_warns():
    """The same pullback without corroborating sell pressure or a falling
    volume label is a warning, not an exit - it awaits confirmation next
    cycle rather than selling on a single noisy tick."""
    position = {"entry_price": 1, "highest_price_since_entry": 1.3}
    row = candidate(price=1.1, price_change_m5_pct=-2, buys_m5=10, sells_m5=8,
                     volume_label="UNKNOWN")
    review = assess_exit(position, row, ShadowRecoveryPolicy())
    assert review["state"] == "REVERSAL_WARNING"


def test_stagnant_position_exits_after_grace_window_with_no_rise():
    policy = ShadowRecoveryPolicy(stagnation_window_seconds=900)
    position = {"entry_price": 1, "highest_price_since_entry": 1, "opened_at": 1_000}
    row = candidate(price=0.99, price_change_m5_pct=-1, buys_m5=10, sells_m5=10)
    # Before the grace window: not yet judged stagnant, and it's not up
    # either, so it just holds.
    before = assess_exit(position, row, policy, now=1_000 + 899)
    assert before["state"] == "HOLD"
    # After the window, still no rise and no positive momentum: exit.
    after = assess_exit(position, row, policy, now=1_000 + 901)
    assert after["state"] == "EXIT"
    assert "no sign of a rise" in " ".join(after["reasons"])


def test_default_stagnation_window_fits_fast_moving_tokens():
    """Pump.fun-style tokens can pump and get rugged within minutes; a
    15-minute default would leave capital exposed far too long."""
    assert ShadowRecoveryPolicy().stagnation_window_seconds <= 300


def test_positive_momentum_prevents_a_stagnant_exit():
    policy = ShadowRecoveryPolicy(stagnation_window_seconds=900)
    position = {"entry_price": 1, "highest_price_since_entry": 1, "opened_at": 1_000}
    row = candidate(price=0.99, price_change_m5_pct=2, buys_m5=10, sells_m5=10)
    review = assess_exit(position, row, policy, now=1_000 + 2_000)
    assert review["state"] == "HOLD"


def test_missing_opened_at_never_triggers_a_stagnant_exit():
    position = {"entry_price": 1, "highest_price_since_entry": 1}
    row = candidate(price=0.99, price_change_m5_pct=-1, buys_m5=10, sells_m5=10)
    review = assess_exit(position, row, ShadowRecoveryPolicy(), now=10**9)
    assert review["state"] == "HOLD"


def test_persisted_high_water_partial_sell_and_performance_survive_restart(tmp_path):
    path = tmp_path / "capital.json"
    book = CapitalBook(path)
    book.initialize(30)
    book.reserve_shadow_buy(agent_id="hunter-v1", mint=MINT, symbol="A", amount_usd=5,
                            entry_price=1, price_currency="USD", entry_liquidity_usd=20_000)
    book.mark_shadow_position("hunter-v1", MINT, 2)
    restarted = CapitalBook(path)
    dipped = restarted.mark_shadow_position("hunter-v1", MINT, 1.8)
    assert dipped["highest_price_since_entry"] == 2
    assert dipped["drawdown_from_post_entry_peak_pct"] == 10
    fill = restarted.close_shadow_position("hunter-v1", MINT, fraction=0.5, stage="PRINCIPAL_RECOVERY")
    assert fill["realized_pnl_usd"] == 2
    account = restarted.public_status()["agents"][0]
    assert account["cash_usd"] == 29.5 and account["reserved_usd"] == 2.5
    assert account["equity_usd"] == 34
    assert restarted.load()["agents"]["hunter-v1"]["positions"][MINT]["principal_recovered"]
    restarted.close_shadow_position("hunter-v1", MINT)
    stats = CapitalBook(path).performance()
    assert stats["completed_trades"] == 1 and stats["winning_trades"] == 1
    assert stats["realized_pnl_usd"] == 4
    assert stats["unrealized_pnl_usd"] == 0
    assert CapitalBook(path).public_status()["agents"][0]["cash_usd"] == 34


def test_shadow_buy_arbitration_overrides_ready_signal(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"generated_at": time.time(), "candidates": [candidate()]}))
    monkeypatch.setenv("RECOMMENDATION_SNAPSHOT_PATH", str(snapshot))
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("AGENT_DECISION_LOG_PATH", str(tmp_path / "log.jsonl"))
    book = CapitalBook(tmp_path / "capital.json")
    book.initialize(30)
    class Model:
        def propose(self, *, role, context):
            assert role == AgentRole.OPPORTUNITY_HUNTER
            return {"action": "BUY", "mint": MINT, "requested_usd": 5, "confidence": 0.1, "thesis": "test"}
    result = shadow_once(Model(), book, core_only=True)["agents"][0]
    assert result["recovery_review"]["state"] == "BUY_READY"
    assert not result["arbitration"]["approved"]
    assert book.public_status()["agents"][0]["cash_usd"] == 30


def test_existing_unmarked_position_can_be_upgraded_without_reset(tmp_path):
    path = tmp_path / "capital.json"
    book = CapitalBook(path)
    book.initialize(30)
    book.reserve_shadow_buy(agent_id="hunter-v1", mint=MINT, symbol="A", amount_usd=5, entry_price=1, price_currency="USD")
    legacy = json.loads(path.read_text())
    for key in ("current_price", "current_value_usd", "highest_price_since_entry", "entry_time"):
        legacy["agents"]["hunter-v1"]["positions"][MINT].pop(key)
    path.write_text(json.dumps(legacy))
    assert CapitalBook(path).mark_shadow_position("hunter-v1", MINT, 1.2)["highest_price_since_entry"] == 1.2


def test_shadow_cycle_marks_then_closes_confirmed_reversal_without_live_order(tmp_path, monkeypatch):
    path = tmp_path / "capital.json"
    book = CapitalBook(path)
    book.initialize(30)
    book.reserve_shadow_buy(agent_id="hunter-v1", mint=MINT, symbol="A", amount_usd=4,
                            entry_price=1, price_currency="USD", entry_liquidity_usd=20_000)
    book.mark_shadow_position("hunter-v1", MINT, 1.5)
    snapshot = tmp_path / "recommendations.json"
    row = candidate(price=1.15, price_change_m5_pct=-9, buys_m5=2, sells_m5=12,
                    momentum_label="FALLING", volume_label="FALLING", decision="WATCH")
    snapshot.write_text(json.dumps({"generated_at": time.time(), "candidates": [row]}))
    monkeypatch.setenv("RECOMMENDATION_SNAPSHOT_PATH", str(snapshot))
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("AGENT_DECISION_LOG_PATH", str(tmp_path / "decisions.jsonl"))
    class Model:
        def propose(self, *, role, context):
            return {"action": "WATCH", "mint": MINT, "confidence": 0.8, "thesis": "wait"}
    output = shadow_once(Model(), book, core_only=True)
    assert output["live_execution"] is False
    assert output["hunter_position_reviews"][0]["state"] == "EXIT"
    assert output["hunter_position_reviews"][0]["shadow_fill"]["exit_value_usd"] == 4.6
    assert CapitalBook(path).public_status()["agents"][0]["cash_usd"] == 30.6
    assert CapitalBook(path).performance()["completed_trades"] == 1


def test_shallow_pullback_without_quote_evidence_never_buys(tmp_path, monkeypatch):
    path = tmp_path / "capital.json"
    book = CapitalBook(path)
    book.initialize(30)
    snapshot = tmp_path / "recommendations.json"
    snapshot.write_text(json.dumps({"generated_at": time.time(), "candidates": [candidate(pullback_from_peak_pct=1, peak_price=0.91)]}))
    monkeypatch.setenv("RECOMMENDATION_SNAPSHOT_PATH", str(snapshot))
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("AGENT_DECISION_LOG_PATH", str(tmp_path / "decisions.jsonl"))
    class Model:
        def propose(self, *, role, context):
            return {"action": "BUY", "mint": MINT, "requested_usd": 5, "confidence": 0.9, "thesis": "test"}
    output = shadow_once(Model(), book, core_only=True)
    assert not output["agents"][0]["arbitration"]["approved"]
    assert book.public_status()["agents"][0]["cash_usd"] == 30


def test_exit_is_capped_by_arbiter_with_remaining_position(tmp_path, monkeypatch):
    book = CapitalBook(tmp_path / "capital.json")
    book.initialize(30)
    book.reserve_shadow_buy(agent_id="hunter-v1", mint=MINT, symbol="A", amount_usd=5,
                            entry_price=1, price_currency="USD", entry_liquidity_usd=20_000)
    book.mark_shadow_position("hunter-v1", MINT, 2)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"generated_at": time.time(), "candidates": [
        candidate(price=1.5, decision="WATCH", price_change_m5_pct=-9,
                  buys_m5=1, sells_m5=10, momentum_label="FALLING")]}))
    monkeypatch.setenv("RECOMMENDATION_SNAPSHOT_PATH", str(snapshot))
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("AGENT_DECISION_LOG_PATH", str(tmp_path / "log.jsonl"))
    class Model:
        def propose(self, *, role, context):
            return {"action": "WATCH", "mint": MINT, "confidence": 0.9, "thesis": "test"}
    review = shadow_once(Model(), book, core_only=True)["hunter_position_reviews"][0]
    assert review["state"] == "EXIT"
    assert review["arbitration"]["approved_usd"] == 5
    assert review["shadow_fill"]["stage"] == "EXIT_CHUNK"
    assert review["remaining_position"] is not None
    assert book.public_status()["agents"][0]["open_positions"] == 1


def test_open_position_is_marked_from_tracked_quote_outside_shortlist(tmp_path, monkeypatch):
    book = CapitalBook(tmp_path / "capital.json")
    book.initialize(30)
    book.reserve_shadow_buy(agent_id="hunter-v1", mint=MINT, symbol="A", amount_usd=5,
                            entry_price=1, price_currency="USD")
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"generated_at": time.time(), "candidates": [],
                                    "tracked_candidates": [candidate(price=1.2)]}))
    monkeypatch.setenv("RECOMMENDATION_SNAPSHOT_PATH", str(snapshot))
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_PATH", str(tmp_path / "missing.json"))
    output = shadow_once(None, book, core_only=True)
    assert output["hunter_position_reviews"][0]["post_entry_peak"] == 1.2
    assert book.public_status()["agents"][0]["equity_usd"] == 31
