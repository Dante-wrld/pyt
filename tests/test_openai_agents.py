from types import SimpleNamespace

import pytest

from solana_launch_guard.agents import AgentRole
from solana_launch_guard.agent_cli import build_parser, friendly_api_error
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


def test_credit_error_is_explained_without_a_traceback_message():
    error = RuntimeError("You have no credits remaining")
    message = friendly_api_error(error)
    assert "credits are exhausted" in message
    assert "No trade was executed" in message
