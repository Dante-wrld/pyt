"""Persisted trial limits are enforced even across independent processes."""
import sqlite3
import time

import pytest

from solana_launch_guard.live_trial_ledger import LiveTrialLedger, TrialHalted


def test_budgets_and_idempotency_survive_reopen(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    path = tmp_path / "trial.sqlite"
    book = LiveTrialLedger(path)
    book.start(now=100)
    for index in range(6):
        book.reserve_buy(intent=f"h{index}", agent="hunter-v1", mint=f"mint-{index}", requested_cents=500,
                         approved_cents=500, now=101 + index)
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        book.reserve_buy(intent="h0", agent="hunter-v1", mint="mint", requested_cents=500,
                         approved_cents=500, now=110)
    with pytest.raises(ValueError, match="budget"):
        book.reserve_buy(intent="h6", agent="hunter-v1", mint="mint", requested_cents=100,
                         approved_cents=100, now=110)
    for kwargs in ({"agent": "portfolio-v1"}, {"requested_cents": 100, "approved_cents": 101},
                   {"requested_cents": 800, "approved_cents": 501}):
        values = dict(intent="bad", agent="copy-v1", mint="mint", requested_cents=500,
                      approved_cents=500, now=110)
        values.update(kwargs)
        with pytest.raises(ValueError):
            book.reserve_buy(**values)
    book.close()

    book = LiveTrialLedger(path)
    assert book.status(now=110)["agents"]["hunter-v1"]["remaining_buy_cap_cents"] == 0
    assert book.status(now=110)["total_remaining_buy_cap_cents"] == 3000
    with pytest.raises(TrialHalted, match="already exists"):
        book.start(now=110)
    book.close()


def test_uncertain_submission_stays_reserved_until_reconciled(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start(now=100)
    book.reserve_buy(intent="buy", agent="hunter-v1", mint="mint", requested_cents=500,
                     approved_cents=300, now=101)
    book.transition("buy", "SUBMITTED", signature="public-signature")
    book.transition("buy", "UNCERTAIN")
    with pytest.raises(ValueError, match="transition"):
        book.transition("buy", "SUBMITTED", signature="different-signature")
    assert book.status(now=102)["agents"]["hunter-v1"]["gross_buy_committed_cents"] == 300
    with pytest.raises(ValueError, match="bounded fill"):
        book.transition("buy", "CONFIRMED", signature="public-signature", executed_cents=301, verified_on_chain=True)
    with pytest.raises(ValueError, match="independent on-chain"):
        book.transition("buy", "CONFIRMED", signature="public-signature", executed_cents=300)
    book.transition("buy", "CONFIRMED", signature="public-signature", executed_cents=300, verified_on_chain=True)
    book.reserve_sell(intent="sell", agent="hunter-v1", mint="mint", now=102)
    book.transition("sell", "SUBMITTED", signature="sell-signature")
    book.transition("sell", "CONFIRMED", signature="sell-signature", proceeds_cents=450, verified_on_chain=True)
    assert book.status(now=103)["agents"]["hunter-v1"]["remaining_buy_cap_cents"] == 2700
    assert book.status(now=103)["agents"]["hunter-v1"]["confirmed_sell_proceeds_cents"] == 450
    book.close()


def test_failed_reservation_is_released_and_kill_switch_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start(now=100)
    book.reserve_buy(intent="failed", agent="copy-v1", mint="mint", requested_cents=500,
                     approved_cents=500, now=101)
    book.transition("failed", "FAILED")
    assert book.status(now=101)["agents"]["copy-v1"]["remaining_buy_cap_cents"] == 3000
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "true")
    with pytest.raises(TrialHalted, match="kill switch"):
        book.reserve_buy(intent="new", agent="copy-v1", mint="mint", requested_cents=500,
                         approved_cents=500, now=102)
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    with pytest.raises(TrialHalted, match="expired"):
        book.reserve_buy(intent="late", agent="copy-v1", mint="mint", requested_cents=500,
                         approved_cents=500, now=100 + 8 * 3600)
    book.halt("operator requested stop")
    assert book.status(now=103)["status"] == "HALTED"
    with pytest.raises(TrialHalted):
        book.reserve_buy(intent="halted", agent="copy-v1", mint="mint", requested_cents=500,
                         approved_cents=500, now=104)
    book.close()


def test_uncertain_order_requires_explicit_negative_chain_proof(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start(now=100)
    book.reserve_buy(intent="uncertain", agent="copy-v1", mint="mint", requested_cents=200,
                     approved_cents=200, now=101)
    book.transition("uncertain", "SUBMITTED", signature="signature")
    book.transition("uncertain", "UNCERTAIN")
    with pytest.raises(ValueError, match="chain reconciliation"):
        book.transition("uncertain", "FAILED")
    assert book.status(now=102)["total_remaining_buy_cap_cents"] == 5800
    book.transition("uncertain", "FAILED", absent_on_chain=True)
    assert book.status(now=102)["total_remaining_buy_cap_cents"] == 6000
    book.close()


def test_operator_stop_is_seen_without_reloading_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    path = tmp_path / "trial.sqlite"
    first = LiveTrialLedger(path)
    first.start(now=100)
    second = LiveTrialLedger(path)
    second.stop()
    with pytest.raises(TrialHalted):
        first.reserve_buy(intent="late", agent="hunter-v1", mint="mint", requested_cents=500,
                          approved_cents=500, now=101)
    assert LiveTrialLedger(path).status(now=101)["status"] == "HALTED"
    first.close()
    second.close()


def test_confirmed_buy_position_and_peak_survive_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    path = tmp_path / "trial.sqlite"
    book = LiveTrialLedger(path)
    book.start(now=100)
    book.reserve_buy(intent="first", agent="hunter-v1", mint="token", requested_cents=500,
                     approved_cents=500, now=101)
    book.transition("first", "SUBMITTED", signature="signature")
    with pytest.raises(ValueError, match="verified"):
        book.confirm_buy(intent="first", signature="signature", executed_cents=500,
                         quantity_raw=100, decimals=6, entry_price=0.05,
                         entry_liquidity_usd=60000, verified_on_chain=False)
    book.confirm_buy(intent="first", signature="signature", executed_cents=500,
                     quantity_raw=100, decimals=6, entry_price=0.05,
                     entry_liquidity_usd=60000, verified_on_chain=True)
    book.mark_position(agent="hunter-v1", mint="token", price=0.07)
    book.close()
    book = LiveTrialLedger(path)
    assert book.positions("hunter-v1")[0]["peak_price"] == .07
    assert book.mark_position(agent="hunter-v1", mint="token", price=.065)["peak_price"] == .07
    with pytest.raises(ValueError, match="already owned"):
        book.reserve_buy(intent="duplicate-mint", agent="copy-v1", mint="token", requested_cents=500,
                         approved_cents=500, now=102)
    book.reserve_sell(intent="exit", agent="hunter-v1", mint="token", now=102)
    book.transition("exit", "SUBMITTED", signature="exit-signature")
    book.confirm_sell(intent="exit", signature="exit-signature", quantity_raw=100,
                      proceeds_cents=700, verified_on_chain=True)
    assert book.status(now=103)["agents"]["hunter-v1"]["open_positions"] == 0
    assert book.status(now=103)["agents"]["hunter-v1"]["remaining_buy_cap_cents"] == 2500
    performance = book.report(now=103)["agents"]["hunter-v1"]
    assert performance["realized_pnl_cents"] == 200
    assert performance["winning_trades_with_known_basis"] == 1
    assert performance["expectancy_cents"] == 200
    book.close()


def test_a_full_exit_is_watched_afterward_for_renewed_growth(tmp_path, monkeypatch):
    """A mint hunter-v1 fully exits doesn't just vanish - its fill price is
    recorded so a later regrowth-rebuy check can compare against it."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start(now=100)
    book.reserve_buy(intent="buy", agent="hunter-v1", mint="token", requested_cents=500,
                     approved_cents=500, now=101)
    book.transition("buy", "SUBMITTED", signature="buy-sig")
    book.confirm_buy(intent="buy", signature="buy-sig", executed_cents=500,
                     quantity_raw=100, decimals=6, entry_price=0.05,
                     entry_liquidity_usd=60000, verified_on_chain=True)
    assert book.closed_positions_for_regrowth("hunter-v1", max_age_seconds=1000, now=102) == []
    book.reserve_sell(intent="sell", agent="hunter-v1", mint="token", now=102)
    book.transition("sell", "SUBMITTED", signature="sell-sig")
    # 100 raw units at 6 decimals = 0.0001 tokens; 700 cents proceeds -> a
    # fill price of $7/0.0001 = $70,000 - the point is only that it's
    # derived from proceeds_cents/quantity_raw, not asserting a realistic
    # price.
    book.confirm_sell(intent="sell", signature="sell-sig", quantity_raw=100,
                      proceeds_cents=700, verified_on_chain=True)
    watched = book.closed_positions_for_regrowth("hunter-v1", max_age_seconds=1000)
    assert len(watched) == 1
    assert watched[0]["mint"] == "token"
    assert watched[0]["exit_price"] == pytest.approx(7 / 0.0001)
    # confirm_sell always stamps closed_at with the real wall clock (it
    # takes no synthetic `now`, unlike reserve_buy/reserve_sell/start).
    assert watched[0]["closed_at"] == pytest.approx(time.time(), abs=5)
    book.close()


def test_regrowth_watch_expires_after_its_own_age_window(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start(now=100)
    book.reserve_buy(intent="buy", agent="hunter-v1", mint="token", requested_cents=500,
                     approved_cents=500, now=101)
    book.transition("buy", "SUBMITTED", signature="buy-sig")
    book.confirm_buy(intent="buy", signature="buy-sig", executed_cents=500,
                     quantity_raw=100, decimals=6, entry_price=0.05,
                     entry_liquidity_usd=60000, verified_on_chain=True)
    book.reserve_sell(intent="sell", agent="hunter-v1", mint="token", now=102)
    book.transition("sell", "SUBMITTED", signature="sell-sig")
    book.confirm_sell(intent="sell", signature="sell-sig", quantity_raw=100,
                      proceeds_cents=700, verified_on_chain=True)
    assert len(book.closed_positions_for_regrowth("hunter-v1", max_age_seconds=1000)) == 1
    assert book.closed_positions_for_regrowth("hunter-v1", max_age_seconds=1000, now=time.time() + 2000) == []
    book.close()


def test_a_regrowth_rebuy_is_tagged_with_its_origin_and_clears_the_watch(tmp_path, monkeypatch):
    """origin distinguishes a hunter-sourced discovery from a portfolio-
    flagged regrowth re-entry, and a fresh buy (from either path) retires
    the mint's closed_positions watch so it isn't double-counted."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start(now=100)
    book.reserve_buy(intent="buy1", agent="hunter-v1", mint="token", requested_cents=500,
                     approved_cents=500, now=101)
    book.transition("buy1", "SUBMITTED", signature="buy1-sig")
    book.confirm_buy(intent="buy1", signature="buy1-sig", executed_cents=500,
                     quantity_raw=100, decimals=6, entry_price=0.05,
                     entry_liquidity_usd=60000, verified_on_chain=True)
    book.reserve_sell(intent="sell1", agent="hunter-v1", mint="token", now=102)
    book.transition("sell1", "SUBMITTED", signature="sell1-sig")
    book.confirm_sell(intent="sell1", signature="sell1-sig", quantity_raw=100,
                      proceeds_cents=700, verified_on_chain=True)
    assert len(book.closed_positions_for_regrowth("hunter-v1", max_age_seconds=1000)) == 1
    book.reserve_buy(intent="buy2", agent="hunter-v1", mint="token", requested_cents=500,
                     approved_cents=500, now=103)
    book.transition("buy2", "SUBMITTED", signature="buy2-sig")
    book.confirm_buy(intent="buy2", signature="buy2-sig", executed_cents=500,
                     quantity_raw=100, decimals=6, entry_price=0.07,
                     entry_liquidity_usd=60000, verified_on_chain=True, origin="regrowth")
    assert book.positions("hunter-v1")[0]["origin"] == "regrowth"
    assert book.closed_positions_for_regrowth("hunter-v1", max_age_seconds=1000) == []
    book.close()


def test_only_hunter_v1_exits_are_watched_for_regrowth(tmp_path, monkeypatch):
    """Only hunter-v1's own regrowth mechanism ever reads closed_positions
    (see decide_regrowth_rebuy, scoped to agent='hunter-v1') - a copy-v1
    exit has nothing that would ever look at its row, so it shouldn't be
    recorded there at all."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start(now=100)
    book.reserve_buy(intent="buy", agent="copy-v1", mint="token", requested_cents=500,
                     approved_cents=500, now=101)
    book.transition("buy", "SUBMITTED", signature="buy-sig")
    book.confirm_buy(intent="buy", signature="buy-sig", executed_cents=500,
                     quantity_raw=100, decimals=6, entry_price=0.05,
                     entry_liquidity_usd=60000, verified_on_chain=True)
    book.reserve_sell(intent="sell", agent="copy-v1", mint="token", now=102)
    book.transition("sell", "SUBMITTED", signature="sell-sig")
    book.confirm_sell(intent="sell", signature="sell-sig", quantity_raw=100,
                      proceeds_cents=700, verified_on_chain=True)
    assert book.closed_positions_for_regrowth("copy-v1", max_age_seconds=1000) == []
    assert book.closed_positions_for_regrowth("hunter-v1", max_age_seconds=1000) == []
    book.close()


def test_partial_profit_stages_and_remaining_cost_survive_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    path = tmp_path / "stages.sqlite"
    book = LiveTrialLedger(path)
    book.start(now=100)
    book.reserve_buy(intent="entry", agent="hunter-v1", mint="token", requested_cents=500,
                     approved_cents=500, now=101)
    book.transition("entry", "SUBMITTED", signature="buy-signature")
    book.confirm_buy(intent="entry", signature="buy-signature", executed_cents=500,
                     quantity_raw=100, decimals=0, entry_price=.05,
                     entry_liquidity_usd=60_000, verified_on_chain=True)
    first = "live-trial:hunter-v1:sell:token:TAKE_PARTIAL:PRINCIPAL"
    book.reserve_sell(intent=first, agent="hunter-v1", mint="token", now=102)
    book.transition(first, "SUBMITTED", signature="sell-one")
    book.confirm_sell(intent=first, signature="sell-one", quantity_raw=50,
                      proceeds_cents=500, verified_on_chain=True)
    book.close()
    book = LiveTrialLedger(path)
    position = book.positions("hunter-v1")[0]
    assert position["principal_recovered"] == 1
    assert position["second_stage_taken"] == 0
    assert position["cost_cents"] == 250
    assert position["quantity_raw"] == 50
    second = "live-trial:hunter-v1:sell:token:TAKE_PARTIAL:SECOND_STAGE"
    book.reserve_sell(intent=second, agent="hunter-v1", mint="token", now=102)
    book.transition(second, "SUBMITTED", signature="sell-two")
    book.confirm_sell(intent=second, signature="sell-two", quantity_raw=25,
                      proceeds_cents=400, verified_on_chain=True)
    assert book.positions("hunter-v1")[0]["second_stage_taken"] == 1
    with pytest.raises(sqlite3.IntegrityError):
        book.reserve_sell(intent=second, agent="hunter-v1", mint="token", now=103)
    book.close()
