import json
import time

from solana_launch_guard.agent_capital import CapitalBook
from solana_launch_guard.agents import AgentRole
from solana_launch_guard.agent_cli import shadow_once
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


def test_zero_confirmations_and_absent_activity_remain_watch():
    result = assess_entry(candidate(entry_confirmation_count=0, volume_label="UNKNOWN"), ShadowRecoveryPolicy())
    assert result["decision"] == "WATCH"
    assert "entry confirmations 0/3" in result["reasons"]


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
