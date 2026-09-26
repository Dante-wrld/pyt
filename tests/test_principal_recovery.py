"""A principal sale that fills short must not switch off the loss protections.

Reported 2026-09-26: a $5 buy followed by a PRINCIPAL-stage sale that
returned $4.50 marked principal_recovered, which disabled the hard stop and
the lock rule while $0.50 of the stake was still at risk.
"""
import time

import pytest
from solana_launch_guard.agent_capital import CapitalBook
from solana_launch_guard.hunter_shadow_strategy import ShadowRecoveryPolicy, assess_exit
from solana_launch_guard.live_trial_ledger import (
    LiveTrialLedger,
    principal_sale_fraction,
    principal_secured,
)

MINT = "M" * 44


def _ledger_with_position(tmp_path, monkeypatch, cost_cents=500):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    book.reserve_buy(intent="buy", agent="hunter-v1", mint=MINT,
                     requested_cents=cost_cents, approved_cents=cost_cents)
    book.transition("buy", "SUBMITTED", signature="sig-buy")
    book.confirm_buy(intent="buy", signature="sig-buy", executed_cents=cost_cents,
                     quantity_raw=1_000_000, decimals=6, entry_price=0.005,
                     entry_liquidity_usd=80_000, verified_on_chain=True)
    return book


def _sell(book, *, stage, quantity_raw, proceeds_cents, n=1):
    intent = f"sell-{n}:{stage}"
    book.reserve_sell(intent=intent, agent="hunter-v1", mint=MINT)
    book.transition(intent, "SUBMITTED", signature=f"sig-{n}")
    book.confirm_sell(intent=intent, signature=f"sig-{n}", quantity_raw=quantity_raw,
                      proceeds_cents=proceeds_cents, verified_on_chain=True)
    [position] = book.positions("hunter-v1")
    return position


def test_short_principal_sale_keeps_the_stake_marked_at_risk(tmp_path, monkeypatch):
    book = _ledger_with_position(tmp_path, monkeypatch)
    position = _sell(book, stage="PRINCIPAL", quantity_raw=500_000, proceeds_cents=450)
    assert position["principal_recovered"] == 1        # the stage ran...
    assert position["principal_secured"] is False       # ...but $0.50 is still at risk
    assert position["entry_cost_cents"] == 500 and position["proceeds_cents"] == 450
    book.close()


def test_later_sales_that_cover_the_stake_secure_it(tmp_path, monkeypatch):
    book = _ledger_with_position(tmp_path, monkeypatch)
    _sell(book, stage="PRINCIPAL", quantity_raw=500_000, proceeds_cents=450, n=1)
    position = _sell(book, stage="SECOND_STAGE", quantity_raw=250_000,
                     proceeds_cents=300, n=2)
    assert position["principal_secured"] is True        # 450 + 300 >= 500
    book.close()


def test_even_a_few_cents_short_is_not_secured(tmp_path, monkeypatch):
    book = _ledger_with_position(tmp_path, monkeypatch)
    position = _sell(book, stage="PRINCIPAL", quantity_raw=500_000, proceeds_cents=492)
    assert position["principal_secured"] is False       # no shortfall tolerance
    book.close()


def test_exactly_the_stake_back_is_secured(tmp_path, monkeypatch):
    book = _ledger_with_position(tmp_path, monkeypatch)
    position = _sell(book, stage="PRINCIPAL", quantity_raw=500_000, proceeds_cents=500)
    assert position["principal_secured"] is True
    book.close()


def test_principal_sale_targets_the_stake_plus_a_buffer():
    # $5 stake worth $10: sell 51.5% so fees and slippage still leave $5 back.
    assert principal_sale_fraction(5, 10) == pytest.approx(0.515)
    assert principal_sale_fraction(5, 5) == 1.0         # capped at the whole bag
    assert principal_sale_fraction(5, 0) == 1.0


def test_positions_from_before_the_migration_are_backfilled(tmp_path, monkeypatch):
    book = _ledger_with_position(tmp_path, monkeypatch)
    _sell(book, stage="PRINCIPAL", quantity_raw=500_000, proceeds_cents=450)
    # Simulate a row written before entry cost and proceeds were tracked.
    book.db.execute("UPDATE positions SET entry_cost_cents=NULL, proceeds_cents=0")
    book.db.commit()
    book.close()
    reopened = LiveTrialLedger(tmp_path / "trial.sqlite")
    [position] = reopened.positions("hunter-v1")
    assert position["entry_cost_cents"] == 500
    assert position["proceeds_cents"] == 450
    assert position["principal_recovered"] == 1
    assert position["principal_secured"] is False       # not trusting the label
    reopened.close()


def test_rows_from_before_the_migration_fall_back_to_the_stage_flag():
    assert principal_secured({"entry_cost_cents": None, "principal_recovered": 1})
    assert not principal_secured({"entry_cost_cents": None, "principal_recovered": 0})


def _exit_state(*, recovered, secured, price):
    policy = ShadowRecoveryPolicy(
        stop_loss_pct=20, lock_after_gain_pct=10, lock_stop_pct=8
    )
    position = {"entry_price": 1.0, "highest_price_since_entry": 2.0,
                "entry_liquidity_usd": 80_000, "opened_at": time.time() - 60,
                "principal_recovered": recovered, "second_stage_taken": False}
    if secured is not None:
        position["principal_secured"] = secured
    quote = {"price": price, "liquidity_usd": 80_000, "buys_m5": 30, "sells_m5": 20,
             "price_change_m5_pct": -1.0}
    return assess_exit(position, quote, policy)


def test_protections_follow_the_money_not_the_label():
    # At -25% the hard stop fires while the stake is still at risk...
    assert _exit_state(recovered=True, secured=False, price=0.75)["state"] == "EXIT"
    # ...and is off once it is actually back.
    assert _exit_state(recovered=True, secured=True, price=0.75)["state"] != "EXIT"
    # Callers that do not track proceeds keep the old stage-flag behavior.
    assert _exit_state(recovered=True, secured=None, price=0.75)["state"] != "EXIT"


def test_stage_order_still_advances_after_a_short_principal_sale():
    # At 3.5x with the principal stage taken but not secured, the ladder asks
    # for the SECOND stage - it does not retry the PRINCIPAL stage (whose
    # intent is already confirmed and would halt the trial).
    review = _exit_state(recovered=True, secured=False, price=3.5)
    assert review["state"] == "TAKE_PARTIAL"
    assert "second profit stage" in review["reasons"][-1]


def test_shadow_book_applies_the_same_rule(tmp_path):
    book = CapitalBook(tmp_path / "capital.json")
    book.initialize(30)
    book.reserve_shadow_buy(agent_id="hunter-v1", mint=MINT, symbol="M", amount_usd=5,
                            entry_price=1.0, price_currency="USD")
    book.mark_shadow_position("hunter-v1", MINT, 2.0)
    # Sell half at 2x with 10% slippage: $10 * 0.5 * 0.9 = $4.50 back on $5.
    book.close_shadow_position("hunter-v1", MINT, fraction=0.5,
                               stage="PRINCIPAL_RECOVERY", slippage_pct=10)
    position = book.load()["agents"]["hunter-v1"]["positions"][MINT]
    assert position["principal_recovered"] is True
    assert position["principal_secured"] is False
    assert position["recovered_usd"] == pytest.approx(4.5)
