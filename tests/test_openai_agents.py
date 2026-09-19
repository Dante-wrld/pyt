from types import SimpleNamespace

import pytest
import json
import time

from solana_launch_guard.agents import AgentRole
from solana_launch_guard.agent_cli import build_parser, friendly_api_error, _priced_sell_signal, _solana_opportunity, _snapshot_is_fresh, shadow_once
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
    recommendations.write_text(json.dumps({"generated_at": time.time(), "candidates": [{"chain": "solana", "mint": "A" * 32, "symbol": "A", "decision": "BUY ZONE", "price": 1, "liquidity_usd": 20000}]}))
    portfolio.write_text(json.dumps({"generated_at": time.time(), "signals": [{"chain": "solana", "token_address": "B" * 32, "decision": "EXIT WARNING", "current_price": 1, "current_value_usd": 2, "liquidity_usd": 20000}]}))
    monkeypatch.setenv("RECOMMENDATION_SNAPSHOT_PATH", str(recommendations))
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_PATH", str(portfolio))
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


def test_manager_reads_loss_reviews_but_cannot_execute_rebuy(tmp_path, monkeypatch):
    portfolio = tmp_path / "portfolio.json"
    review = {"sale_id": "tx1", "token_address": "B" * 32,
              "decision": "REBUY REVIEW", "cost_usd": 5, "proceeds_usd": 3}
    portfolio.write_text(json.dumps({"generated_at": time.time(),
                                     "signals": [], "loss_sale_reviews": [review]}))
    monkeypatch.setenv("RECOMMENDATION_SNAPSHOT_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_PATH", str(portfolio))
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
