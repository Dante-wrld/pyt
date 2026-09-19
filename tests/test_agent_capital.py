import json

import pytest

from solana_launch_guard.agent_capital import CapitalBook


def test_initializes_three_isolated_thirty_dollar_accounts(tmp_path):
    book = CapitalBook(tmp_path / "capital.json")
    result = book.initialize(30)
    assert result["total_starting_capital_usd"] == 90
    assert [row.cash_usd for row in book.accounts()] == [30, 30, 30]
    assert result["live_execution"] is False


def test_initialization_is_idempotent_and_never_reallocates(tmp_path):
    book = CapitalBook(tmp_path / "capital.json")
    first = book.initialize(30)
    assert book.initialize(30) == first
    with pytest.raises(ValueError, match="not overwritten"):
        book.initialize(50)


def test_shadow_buy_reserves_only_approved_agent_cash(tmp_path):
    book = CapitalBook(tmp_path / "capital.json")
    book.initialize(30)
    book.reserve_shadow_buy(
        agent_id="hunter-v1",
        mint="mint-a",
        symbol="A",
        amount_usd=5,
        entry_price=0.1,
        price_currency="USD",
    )
    status = {row.agent_id: row for row in book.accounts()}
    assert status["hunter-v1"].cash_usd == 25
    assert status["hunter-v1"].reserved_usd == 5
    assert status["copy-v1"].cash_usd == 30


def test_shadow_limits_block_oversize_and_third_position(tmp_path):
    book = CapitalBook(tmp_path / "capital.json")
    book.initialize(30)
    with pytest.raises(ValueError, match=r"\$5 maximum"):
        book.reserve_shadow_buy(
            agent_id="hunter-v1",
            mint="large",
            symbol="L",
            amount_usd=6,
            entry_price=1,
            price_currency="USD",
        )
    for mint in ("one", "two"):
        book.reserve_shadow_buy(
            agent_id="hunter-v1",
            mint=mint,
            symbol=mint,
            amount_usd=5,
            entry_price=1,
            price_currency="USD",
        )
    with pytest.raises(ValueError, match="two open positions"):
        book.reserve_shadow_buy(
            agent_id="hunter-v1",
            mint="three",
            symbol="three",
            amount_usd=5,
            entry_price=1,
            price_currency="USD",
        )


def test_capital_file_contains_no_live_execution_permission(tmp_path):
    path = tmp_path / "capital.json"
    CapitalBook(path).initialize(30)
    payload = json.loads(path.read_text())
    assert payload["mode"] == "shadow"
    assert payload["live_execution"] is False
