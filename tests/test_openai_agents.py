from types import SimpleNamespace

import pytest
import json
import time

from solana_launch_guard.agents import AgentRole
from solana_launch_guard.agent_cli import build_parser, friendly_api_error, _priced_sell_signal, _solana_opportunity, _snapshot_is_fresh, shadow_once
from solana_launch_guard.agent_cli import HunterRulesModel
from solana_launch_guard.agent_capital import CapitalBook
from solana_launch_guard.openai_agents import (
    OpenAIProposalModel,
    ProposalOutput,
    connection_test,
    sanitize_context,
)


class FakeResponses:
    def __init__(self, output):
        self.output = output
        self.kwargs = None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(output_parsed=self.output)


def fake_model(output):
    model = object.__new__(OpenAIProposalModel)
    model.model = "test-model"
    model.client = SimpleNamespace(responses=FakeResponses(output))
    return model


def test_context_redacts_credentials_recursively():
    clean = sanitize_context(
        {"market": {"api_key": "nope", "private_key": "nope", "price": 2}}
    )
    assert clean == {
        "market": {"api_key": "[REDACTED]", "private_key": "[REDACTED]", "price": 2}
    }


def test_adapter_uses_structured_nonstored_response():
    output = ProposalOutput(action="WATCH", confidence=0.7, thesis="wait")
    model = fake_model(output)
    result = model.propose(role=AgentRole.OPPORTUNITY_HUNTER, context={"price": 1})
    assert result["action"] == "WATCH"
    assert model.client.responses.kwargs["store"] is False
    assert model.client.responses.kwargs["text_format"] is ProposalOutput


def test_connection_test_requires_harmless_hold():
    model = fake_model(ProposalOutput(action="HOLD", confidence=1, thesis="no position"))
    assert connection_test(model)["connected"] is True


def test_connection_test_fails_closed_on_unexpected_action():
    model = fake_model(
        ProposalOutput(
            action="BUY", mint="mint", requested_usd=5, confidence=0.8, thesis="bad test"
        )
    )
    with pytest.raises(RuntimeError, match="expected HOLD"):
        connection_test(model)


def test_agent_cli_requires_one_safe_command():
    assert build_parser().parse_args(["--test-api"]).test_api is True
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
    assert build_parser().parse_args(
        ["--initialize-capital", "30"]
    ).initialize_capital == 30
    assert build_parser().parse_args(["--shadow-once"]).shadow_once is True
    assert build_parser().parse_args(["--shadow-portfolio-sell-once"]).shadow_portfolio_sell_once is True
    assert build_parser().parse_args(["--shadow-core-once"]).shadow_core_once is True
    assert build_parser().parse_args(["--shadow-core-loop"]).shadow_core_loop is True


def test_loop_skips_stale_or_future_snapshots():
    assert _snapshot_is_fresh({"generated_at": 100}, now=109)
    assert not _snapshot_is_fresh({"generated_at": 100}, now=116)
    assert not _snapshot_is_fresh({"generated_at": 101}, now=100)
    assert not _snapshot_is_fresh({}, now=100)


def test_core_cycle_reviews_sell_before_hunter(tmp_path, monkeypatch):
    recommendations = tmp_path / "recommendations.json"
    portfolio = tmp_path / "portfolio.json"
    recommendations.write_text(json.dumps({"generated_at": time.time(), "candidates": [{"chain": "solana", "mint": "A" * 32, "decision": "WATCH", "liquidity_usd": 20000}]}))
    portfolio.write_text(json.dumps({"generated_at": time.time(), "signals": [{"chain": "solana", "token_address": "B" * 32, "decision": "EXIT WARNING", "current_price": 1, "current_value_usd": 2, "liquidity_usd": 20000}]}))
    monkeypatch.setenv("RECOMMENDATION_SNAPSHOT_PATH", str(recommendations))
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_PATH", str(portfolio))
    monkeypatch.setenv("AGENT_COPY_SIGNAL_PATH", str(tmp_path / "missing_copy.json"))
    monkeypatch.setenv("AGENT_DECISION_LOG_PATH", str(tmp_path / "decisions.jsonl"))
    book = CapitalBook(tmp_path / "capital.json")
    book.initialize(30)
    class Model:
        def propose(self, *, role, context):
            return {"action": "HOLD", "mint": "", "confidence": 0.8, "thesis": "test"}
    result = shadow_once(Model(), book, core_only=True)
    assert [item["agent_id"] for item in result["agents"]] == ["portfolio-v1", "hunter-v1"]


def test_shadow_buy_updates_reported_cash_and_sell_approval_does_not(tmp_path, monkeypatch):
    recommendations = tmp_path / "recommendations.json"
    portfolio = tmp_path / "portfolio.json"
    recommendations.write_text(json.dumps({"generated_at": time.time(), "candidates": [{"chain": "solana", "mint": "A" * 32, "symbol": "A", "decision": "BUY ZONE", "price": 1, "price_currency": "USD", "peak_price": 1.1, "pullback_from_peak_pct": 9.09, "liquidity_usd": 20000, "initial_liquidity_usd": 20000, "quoted_at": time.time(), "price_change_m5_pct": 3, "momentum_label": "RISING", "volume_label": "RISING", "buys_m5": 20, "sells_m5": 10, "buy_sell_ratio": 2, "risk_label": "MEDIUM", "signal_score": 80, "entry_confirmation_count": 3, "entry_confirmation_required": 3}]}))
    portfolio.write_text(json.dumps({"generated_at": time.time(), "signals": [{"chain": "solana", "token_address": "B" * 32, "decision": "EXIT WARNING", "current_price": 1, "current_value_usd": 2, "liquidity_usd": 20000}]}))
    monkeypatch.setenv("RECOMMENDATION_SNAPSHOT_PATH", str(recommendations))
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_PATH", str(portfolio))
    monkeypatch.setenv("AGENT_COPY_SIGNAL_PATH", str(tmp_path / "missing_copy.json"))
    monkeypatch.setenv("AGENT_DECISION_LOG_PATH", str(tmp_path / "decisions.jsonl"))
    book = CapitalBook(tmp_path / "capital.json")
    book.initialize(30)

    class Model:
        def propose(self, *, role, context):
            if role is AgentRole.OPPORTUNITY_HUNTER:
                return {"action": "BUY", "mint": "A" * 32, "requested_usd": 5, "confidence": 0.9, "thesis": "test"}
            return {"action": "SELL", "mint": "B" * 32, "requested_usd": 2, "confidence": 0.9, "thesis": "test"}

    result = shadow_once(Model(), book, core_only=True)
    sell, buy = result["agents"]
    assert sell["arbitration"]["approved"] is True
    assert sell["shadow_fill"] is None
    assert sell["shadow_balance"]["cash_usd"] == 30
    assert buy["shadow_fill"]["amount_usd"] == 5
    assert buy["shadow_balance"]["cash_usd"] == 25
    assert buy["shadow_balance"]["reserved_usd"] == 5
    assert result["capital"]["agents"][0]["cash_usd"] == 25
    assert book.public_status()["agents"][0]["cash_usd"] == 25


def test_core_loop_includes_copy_trader_when_a_leader_signal_is_fresh(
    tmp_path, monkeypatch
):
    """core_only used to drop copy-v1 outright (agent_cli.py's own
    --shadow-core-once help text said "skip copy trader"); it now only
    drops it when there's nothing for it to evaluate, same as hunter and
    portfolio - so it joins the loop exactly when a leader signal exists."""
    mint = "A" * 32
    recommendations = tmp_path / "recommendations.json"
    copy_signals = tmp_path / "copy_signals.json"
    recommendations.write_text(json.dumps({"generated_at": time.time(), "candidates": [
        {"chain": "solana", "mint": mint, "symbol": "A", "decision": "BUY ZONE",
         "price": 1, "price_currency": "USD", "liquidity_usd": 20000,
         "quoted_at": time.time()},
    ]}))
    copy_signals.write_text(json.dumps({"generated_at": time.time(), "signals": [
        {"leader_wallet": "LEADER_WALLET", "mint": mint, "liquidity_usd": 20000,
         "price_move_since_entry_pct": 5},
    ]}))
    monkeypatch.setenv("RECOMMENDATION_SNAPSHOT_PATH", str(recommendations))
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("AGENT_COPY_SIGNAL_PATH", str(copy_signals))
    monkeypatch.setenv("AGENT_DECISION_LOG_PATH", str(tmp_path / "decisions.jsonl"))
    book = CapitalBook(tmp_path / "capital.json")
    book.initialize(30)

    class Model:
        def propose(self, *, role, context):
            if role is AgentRole.COPY_TRADER:
                assert context["observed_leader_trade"]["mint"] == mint
                leader_wallet = context["observed_leader_trade"]["leader_wallet"]
                return {"action": "BUY", "mint": mint, "requested_usd": 5,
                        "confidence": 0.9, "thesis": "follow leader",
                        "leader_wallet": leader_wallet}
            return {"action": "HOLD", "mint": "", "confidence": 0.8, "thesis": "n/a"}

    result = shadow_once(Model(), book, core_only=True)
    [copy] = [a for a in result["agents"] if a["agent_id"] == "copy-v1"]
    assert copy["arbitration"]["approved"] is True
    assert copy["shadow_fill"]["amount_usd"] == 5


def test_core_loop_copy_buy_is_rejected_without_a_fresh_watched_quote(
    tmp_path, monkeypatch
):
    """copy-v1's proposed mint can age off the board between its own
    signal firing and this cycle's quote refresh; this must be a clean
    rejection, not a KeyError in the buy-fill path."""
    mint = "A" * 32
    copy_signals = tmp_path / "copy_signals.json"
    copy_signals.write_text(json.dumps({"generated_at": time.time(), "signals": [
        {"leader_wallet": "LEADER_WALLET", "mint": mint, "liquidity_usd": 20000,
         "price_move_since_entry_pct": 5},
    ]}))
    monkeypatch.setenv("RECOMMENDATION_SNAPSHOT_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_PATH", str(tmp_path / "missing2.json"))
    monkeypatch.setenv("AGENT_COPY_SIGNAL_PATH", str(copy_signals))
    monkeypatch.setenv("AGENT_DECISION_LOG_PATH", str(tmp_path / "decisions.jsonl"))
    book = CapitalBook(tmp_path / "capital.json")
    book.initialize(30)

    class Model:
        def propose(self, *, role, context):
            return {"action": "BUY", "mint": mint, "requested_usd": 5,
                    "confidence": 0.9, "thesis": "follow leader",
                    "leader_wallet": "LEADER_WALLET"}

    result = shadow_once(Model(), book, core_only=True)
    [copy] = [a for a in result["agents"] if a["agent_id"] == "copy-v1"]
    assert copy["arbitration"]["approved"] is False
    assert "buy mint lacks a fresh watched quote" in copy["arbitration"]["reasons"]
    assert copy["shadow_fill"] is None


def test_manager_reads_loss_reviews_but_cannot_execute_rebuy(tmp_path, monkeypatch):
    portfolio = tmp_path / "portfolio.json"
    review = {"sale_id": "tx1", "token_address": "B" * 32,
              "decision": "REBUY REVIEW", "cost_usd": 5, "proceeds_usd": 3}
    portfolio.write_text(json.dumps({"generated_at": time.time(),
                                     "signals": [], "loss_sale_reviews": [review]}))
    monkeypatch.setenv("RECOMMENDATION_SNAPSHOT_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_PATH", str(portfolio))
    monkeypatch.setenv("AGENT_COPY_SIGNAL_PATH", str(tmp_path / "missing_copy.json"))
    monkeypatch.setenv("AGENT_DECISION_LOG_PATH", str(tmp_path / "decisions.jsonl"))
    book = CapitalBook(tmp_path / "capital.json")
    book.initialize(30)
    class Model:
        def propose(self, *, role, context):
            assert context["loss_sale_reviews"] == [review]
            return {"action": "REBUY", "mint": "B" * 32,
                    "requested_usd": 0, "confidence": 0.9, "thesis": "review only"}
    result = shadow_once(Model(), book, core_only=True)
    manager = result["agents"][0]
    assert manager["proposal"]["action"] == "REBUY"
    assert manager["arbitration"]["approved"] is False
    assert manager["shadow_fill"] is None
    assert result["capital"]["agents"][1]["cash_usd"] == 30


def test_solana_opportunity_skips_other_chains_and_avoid():
    ethereum = {"chain": "ethereum", "decision": "BUY NOW", "mint": "0x" + "a" * 40}
    avoid = {"chain": "solana", "decision": "AVOID", "mint": "B" * 32}
    solana = {"chain": "solana", "decision": "BUY ZONE", "mint": "C" * 32}
    assert _solana_opportunity([ethereum, avoid, solana]) is solana
    assert _solana_opportunity([ethereum, avoid]) == {}


def test_portfolio_sell_selection_skips_unpriced_and_non_solana():
    unpriced = {"chain": "solana", "decision": "UNPRICED", "current_price": None}
    ethereum = {"chain": "ethereum", "decision": "EXIT WARNING", "current_price": 1, "current_value_usd": 10, "liquidity_usd": 10000}
    partial = {"chain": "solana", "decision": "TAKE PARTIAL", "current_price": 1, "current_value_usd": 4, "liquidity_usd": 10000}
    exit_warning = {"chain": "solana", "decision": "EXIT WARNING", "current_price": 2, "current_value_usd": 2, "liquidity_usd": 5000}
    assert _priced_sell_signal([unpriced, ethereum, partial, exit_warning]) is exit_warning
    assert _priced_sell_signal([unpriced, ethereum]) == {}


def test_credit_error_is_explained_without_a_traceback_message():
    error = RuntimeError("You have no credits remaining")
    message = friendly_api_error(error)
    assert "credits are exhausted" in message
    assert "No trade was executed" in message


def _watched(mint, score, state):
    return {"mint": mint, "signal_score": score}, state


def _hunter_context(*pairs):
    watched = [w for w, _ in pairs]
    reviews = {w["mint"]: {"state": s} for w, s in pairs}
    return {"recovery_reviews": reviews, "watched_candidates": watched}


def test_hunter_rules_model_picks_the_best_buy_ready_candidate_with_no_api_call():
    model = HunterRulesModel()
    context = _hunter_context(
        _watched("A" * 32, 60, "WATCH"),
        _watched("B" * 32, 90, "BUY_READY"),
        _watched("C" * 32, 75, "BUY_READY"),
    )
    proposal = model.propose(role=AgentRole.OPPORTUNITY_HUNTER, context=context)
    assert proposal["action"] == "BUY"
    assert proposal["mint"] == "B" * 32
    assert proposal["requested_usd"] == HunterRulesModel.HUNTER_ORDER_USD
    assert proposal["confidence"] >= 0.65  # clears RiskPolicy.minimum_confidence


def test_hunter_rules_model_holds_with_no_buy_ready_candidate():
    model = HunterRulesModel()
    context = _hunter_context(_watched("A" * 32, 99, "WATCH"))
    proposal = model.propose(role=AgentRole.OPPORTUNITY_HUNTER, context=context)
    assert proposal["action"] == "HOLD"


def test_hunter_rules_model_delegates_other_roles_to_its_fallback():
    class FallbackSpy:
        def __init__(self):
            self.calls = []

        def propose(self, *, role, context):
            self.calls.append(role)
            return {"action": "HOLD", "mint": "", "confidence": 1, "thesis": "fallback"}

    fallback = FallbackSpy()
    model = HunterRulesModel(fallback=fallback)
    model.propose(role=AgentRole.PORTFOLIO_MANAGER, context={})
    model.propose(role=AgentRole.COPY_TRADER, context={})
    assert fallback.calls == [AgentRole.PORTFOLIO_MANAGER, AgentRole.COPY_TRADER]


def test_a_failing_fallback_model_holds_that_role_instead_of_raising():
    class Broken:
        def propose(self, *, role, context):
            raise RuntimeError("You have no credits remaining")

    model = HunterRulesModel(fallback=Broken())
    proposal = model.propose(role=AgentRole.PORTFOLIO_MANAGER, context={})
    assert proposal["action"] == "HOLD"
    assert "credits are exhausted" in proposal["thesis"]


def test_hunter_still_buys_when_the_other_roles_model_is_down(tmp_path, monkeypatch):
    """portfolio-v1 always has context (every holding) and is asked before
    hunter; its model failing used to abort the whole cycle, so hunter never
    got evaluated even though it needs no model."""
    mint = "A" * 32
    now = time.time()
    (tmp_path / "rec.json").write_text(json.dumps({"generated_at": now, "candidates": [
        {"chain": "solana", "mint": mint, "symbol": "A", "decision": "BUY ZONE",
         "price": 1, "price_currency": "USD", "peak_price": 1.1,
         "pullback_from_peak_pct": 9.09, "liquidity_usd": 20000,
         "initial_liquidity_usd": 20000, "quoted_at": now,
         "pair_created_at_ms": now * 1000 - 10 * 86_400_000,
         "price_change_m5_pct": 3, "momentum_label": "RISING",
         "volume_label": "RISING", "buys_m5": 20, "sells_m5": 10,
         "buy_sell_ratio": 2, "risk_label": "MEDIUM", "signal_score": 80,
         "entry_confirmation_count": 3, "entry_confirmation_required": 3}]}))
    (tmp_path / "pf.json").write_text(json.dumps({"generated_at": now, "signals": [
        {"chain": "solana", "token_address": "B" * 32, "decision": "HOLD",
         "current_price": 1, "current_value_usd": 2, "liquidity_usd": 20000}]}))
    monkeypatch.setenv("RECOMMENDATION_SNAPSHOT_PATH", str(tmp_path / "rec.json"))
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_PATH", str(tmp_path / "pf.json"))
    monkeypatch.setenv("AGENT_DECISION_LOG_PATH", str(tmp_path / "d.jsonl"))
    monkeypatch.setenv("ENTRY_ALLOWED_DECISIONS", "BUY ZONE")
    monkeypatch.setenv("ENTRY_MIN_TOKEN_AGE_DAYS", "3")
    book = CapitalBook(tmp_path / "cap.json")
    book.initialize(30)

    class Broken:
        def propose(self, *, role, context):
            raise RuntimeError("You have no credits remaining")

    result = shadow_once(HunterRulesModel(fallback=Broken()), book, core_only=True)
    by_id = {a["agent_id"]: a for a in result["agents"]}
    assert by_id["portfolio-v1"]["proposal"]["action"] == "HOLD"
    assert by_id["hunter-v1"]["arbitration"]["approved"] is True
    assert by_id["hunter-v1"]["shadow_fill"]["amount_usd"] == 5.0


def test_hunter_rules_model_without_a_fallback_holds_other_roles_instead_of_erroring():
    model = HunterRulesModel()
    proposal = model.propose(role=AgentRole.PORTFOLIO_MANAGER, context={})
    assert proposal["action"] == "HOLD"


def test_shadow_once_buys_through_hunter_rules_with_zero_model_calls(
    tmp_path, monkeypatch
):
    """End to end: hunter-v1 reaches a real shadow fill using only
    assess_entry's own BUY_READY verdict, confirming no API call happens on
    hunter's path at all - not even indirectly through a fallback."""
    mint = "A" * 32
    recommendations = tmp_path / "recommendations.json"
    recommendations.write_text(json.dumps({"generated_at": time.time(), "candidates": [
        {"chain": "solana", "mint": mint, "symbol": "A", "decision": "BUY ZONE",
         "price": 1, "price_currency": "USD", "peak_price": 1.1,
         "pullback_from_peak_pct": 9.09, "liquidity_usd": 20000,
         "initial_liquidity_usd": 20000, "quoted_at": time.time(),
         "pair_created_at_ms": time.time() * 1000 - 10 * 86_400_000,
         "price_change_m5_pct": 3, "momentum_label": "RISING",
         "volume_label": "RISING", "buys_m5": 20, "sells_m5": 10,
         "buy_sell_ratio": 2, "risk_label": "MEDIUM", "signal_score": 80,
         "entry_confirmation_count": 3, "entry_confirmation_required": 3},
    ]}))
    monkeypatch.setenv("RECOMMENDATION_SNAPSHOT_PATH", str(recommendations))
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("AGENT_DECISION_LOG_PATH", str(tmp_path / "decisions.jsonl"))
    # Isolated from whatever the live freeze happens to be set to: this
    # test is about HunterRulesModel needing no model call, not about the
    # freeze (test_shadow_live_entry_gate.py already covers that).
    monkeypatch.setenv("ENTRY_ALLOWED_DECISIONS", "BUY ZONE")
    monkeypatch.setenv("ENTRY_MIN_TOKEN_AGE_DAYS", "3")
    book = CapitalBook(tmp_path / "capital.json")
    book.initialize(30)

    result = shadow_once(HunterRulesModel(), book, core_only=True)
    [hunter] = [a for a in result["agents"] if a["agent_id"] == "hunter-v1"]
    assert hunter["arbitration"]["approved"] is True
    assert hunter["shadow_fill"]["amount_usd"] == HunterRulesModel.HUNTER_ORDER_USD
