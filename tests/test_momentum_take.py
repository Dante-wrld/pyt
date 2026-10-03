"""momentum-take-v1 and the early take-profit exit it paper-tests."""

import asyncio
import json
import time

import pytest
from solana_launch_guard.book_comparison import load_books
from solana_launch_guard.hunter_shadow_strategy import ShadowRecoveryPolicy, assess_exit
from solana_launch_guard.momentum_take_shadow import (
    MOMENTUM_TAKE_AGENT_ID,
    MomentumTakeCapitalBook,
    momentum_take_decisions,
    momentum_take_policy,
)
from solana_launch_guard.wide_shadow import WIDE_AGENT_ID, wide_cycle

from test_hunter_shadow_strategy import candidate


def momentum(**changes):
    row = candidate(
        mint="M" * 44,
        decision="MOMENTUM BUY",
        pullback_from_peak_pct=0,
        price=1.0,
        peak_price=1.0,
        buys_m5=60,
        sells_m5=20,
        buy_sell_ratio=3,
        volume_label="RISING",
        entry_confirmation_count=1,
        entry_confirmation_required=1,
    )
    row.update(changes)
    return row


def snapshot(rows):
    now = time.time()
    for row in rows:
        row["quoted_at"] = now
    return json.loads(json.dumps({"generated_at": now, "candidates": rows}))


def run(book, snap, policy, *, now=None):
    async def fetch(mints):
        return {}

    return asyncio.run(
        wide_cycle(
            book,
            snap,
            policy=policy,
            decisions=("MOMENTUM BUY",),
            fetch_quotes=fetch,
            agent_id=MOMENTUM_TAKE_AGENT_ID,
            now=now,
        )
    )


@pytest.fixture
def book(tmp_path):
    b = MomentumTakeCapitalBook(tmp_path / "momentum.json")
    b.initialize(30)
    return b


def position(**changes):
    row = {
        "entry_price": 1.0,
        "highest_price_since_entry": 1.05,
        "opened_at": time.time() - 30,
    }
    row.update(changes)
    return row


QUOTE = {
    "price": 1.05,
    "liquidity_usd": 20_000,
    "price_change_m5_pct": 1,
    "buys_m5": 20,
    "sells_m5": 10,
}


def test_early_take_is_off_by_default():
    assert assess_exit(position(), QUOTE, ShadowRecoveryPolicy())["state"] != "EXIT"


def test_early_take_sells_everything_at_the_target():
    policy = ShadowRecoveryPolicy(early_take_pct=4)
    result = assess_exit(position(), QUOTE, policy)
    assert result["state"] == "EXIT"
    assert any("early take-profit" in r for r in result["reasons"])
    below = assess_exit(position(), {**QUOTE, "price": 1.03}, policy)
    assert below["state"] != "EXIT"


def test_early_take_leaves_house_money_and_stops_alone():
    policy = ShadowRecoveryPolicy(early_take_pct=4)
    recovered = assess_exit(position(principal_recovered=True), QUOTE, policy)
    assert not any("early take" in r for r in recovered["reasons"])
    crash = assess_exit(position(), {**QUOTE, "price": 0.7}, policy)
    assert any("hard stop" in r for r in crash["reasons"])


def test_early_take_env_and_validation(monkeypatch):
    monkeypatch.setenv("EARLY_TAKE_PCT", "4")
    assert ShadowRecoveryPolicy.from_env().early_take_pct == 4
    monkeypatch.setenv("EARLY_TAKE_PCT", "-1")
    with pytest.raises(ValueError):
        ShadowRecoveryPolicy.from_env()


def test_book_policy_applies_only_its_own_exit_settings(monkeypatch):
    monkeypatch.delenv("EARLY_TAKE_PCT", raising=False)
    policy = momentum_take_policy()
    assert policy.early_take_pct == 4
    assert policy.stagnation_window_seconds == 1800
    assert ShadowRecoveryPolicy.from_env().early_take_pct == 0  # live untouched
    monkeypatch.setenv("MOMENTUM_TAKE_PCT", "6")
    monkeypatch.setenv("MOMENTUM_TAKE_STAGNATION_SECONDS", "900")
    tuned = momentum_take_policy()
    assert (tuned.early_take_pct, tuned.stagnation_window_seconds) == (6, 900)
    monkeypatch.setenv("MOMENTUM_TAKE_PCT", "0")
    with pytest.raises(ValueError):
        momentum_take_policy()


def test_decisions_default_to_momentum(monkeypatch):
    monkeypatch.delenv("MOMENTUM_TAKE_DECISIONS", raising=False)
    assert momentum_take_decisions() == ("MOMENTUM BUY",)
    monkeypatch.setenv("MOMENTUM_TAKE_DECISIONS", "momentum buy, buy zone")
    assert momentum_take_decisions() == ("MOMENTUM BUY", "BUY ZONE")


def test_book_buys_paused_momentum_on_paper_and_takes_profit(book):
    policy = momentum_take_policy(ShadowRecoveryPolicy())
    bought = run(book, snapshot([momentum()]), policy)
    assert bought["agent_id"] == MOMENTUM_TAKE_AGENT_ID
    assert bought["entry"]["decision"] == "MOMENTUM BUY"
    positions = book.load()["agents"][MOMENTUM_TAKE_AGENT_ID]["positions"]
    assert "M" * 44 in positions
    up = run(book, snapshot([momentum(price=1.05, peak_price=1.05)]), policy)
    [sold] = [e for e in up["exits"] if "fill" in e]
    assert sold["state"] == "EXIT"
    assert "early take-profit" in sold["reasons"][-1]
    [sale] = book.sell_history(MOMENTUM_TAKE_AGENT_ID, "M" * 44, price_currency="USD")
    assert sale.realized_usd > 0  # +5% beats the 1.2% round-trip cost


def test_thirty_minute_stagnation_window_holds_a_flat_trade(book):
    policy = momentum_take_policy(ShadowRecoveryPolicy())
    start = time.time()
    run(book, snapshot([momentum()]), policy, now=start)
    flat = momentum(price=1.0, price_change_m5_pct=-0.5, momentum_label="RISING")
    ten_min = run(book, snapshot([flat]), policy, now=start + 600)
    assert not [e for e in ten_min["exits"] if "fill" in e]
    default = ShadowRecoveryPolicy()
    assert default.stagnation_window_seconds == 300  # what wide-v1 would use


def test_wide_v1_keeps_its_own_agent(tmp_path):
    from solana_launch_guard.wide_shadow import WideCapitalBook

    wide = WideCapitalBook(tmp_path / "wide.json")
    wide.initialize(30)
    result = asyncio.run(wide_cycle(wide, snapshot([]), policy=ShadowRecoveryPolicy()))
    assert result["agent_id"] == WIDE_AGENT_ID


def test_books_report_includes_momentum_take(tmp_path, book):
    run(book, snapshot([momentum()]), momentum_take_policy(ShadowRecoveryPolicy()))
    results = load_books(
        swing_book=tmp_path / "none.json",
        trend_directory=tmp_path,
        hunter_book=None,
        momentum_take_book=book.path,
    )
    assert any(r.name == "momentum-take-v1" for r in results)


def test_paper_books_default_to_ten_dollars(monkeypatch):
    from solana_launch_guard.wide_shadow import paper_order_usd

    monkeypatch.delenv("PAPER_ORDER_USD", raising=False)
    assert paper_order_usd() == 10
    monkeypatch.setenv("PAPER_ORDER_USD", "7.5")
    assert paper_order_usd() == 7.5
    monkeypatch.setenv("PAPER_ORDER_USD", "0")
    with pytest.raises(ValueError):
        paper_order_usd()


def test_momentum_take_uses_the_fresh_setup_gate(book):
    from solana_launch_guard.wide_fresh_shadow import wide_fresh_cycle

    policy = momentum_take_policy(ShadowRecoveryPolicy())

    async def fetch(mints):
        return {}

    def cycle(row):
        return asyncio.run(wide_fresh_cycle(
            book, snapshot([row]), policy=policy, decisions=("MOMENTUM BUY",),
            fetch_quotes=fetch, agent_id=MOMENTUM_TAKE_AGENT_ID, order_usd=10.0,
        ))

    assert cycle(momentum())["entry"] is not None
    account = book.load()["agents"][MOMENTUM_TAKE_AGENT_ID]
    assert account["positions"]["M" * 44]["allocated_usd"] == 10.0
    sold = cycle(momentum(price=1.05, peak_price=1.05))
    assert any("fill" in e for e in sold["exits"])
    # Same setup, no new high above the board peak at exit: not re-bought.
    again = cycle(momentum(price=1.05, peak_price=1.05))
    assert again["entry"] is None
    assert "fresh_gate" in book.load()["agents"][MOMENTUM_TAKE_AGENT_ID]
    # A new high clears the gate.
    assert cycle(momentum(price=1.2, peak_price=1.2))["entry"] is not None


def test_momentum_take_age_guard_can_differ_from_the_shared_one(monkeypatch):
    from solana_launch_guard.momentum_take_shadow import (
        momentum_take_min_token_age_minutes,
    )
    assert momentum_take_min_token_age_minutes() == 0.0
    monkeypatch.setenv("PAPER_MIN_TOKEN_AGE_MINUTES", "60")
    assert momentum_take_min_token_age_minutes() == 60.0  # follows the shared guard
    monkeypatch.setenv("MOMENTUM_TAKE_MIN_TOKEN_AGE_MINUTES", "0")
    assert momentum_take_min_token_age_minutes() == 0.0   # explicit override wins
    monkeypatch.setenv("MOMENTUM_TAKE_MIN_TOKEN_AGE_MINUTES", "15")
    assert momentum_take_min_token_age_minutes() == 15.0


def test_momentum_take_stop_loss_is_its_own_setting(monkeypatch):
    from solana_launch_guard.momentum_take_shadow import momentum_take_policy
    shared = momentum_take_policy().stop_loss_pct
    monkeypatch.setenv("MOMENTUM_TAKE_STOP_LOSS_PCT", "10")
    monkeypatch.setenv("MOMENTUM_TAKE_PCT", "2")
    monkeypatch.setenv("MOMENTUM_TAKE_STAGNATION_SECONDS", "300")
    policy = momentum_take_policy()
    assert (policy.stop_loss_pct, policy.early_take_pct,
            policy.stagnation_window_seconds) == (10.0, 2.0, 300.0)
    assert shared != 10.0  # unset falls back to the shared stop, not this one


def test_quick_exit_settings_sell_small_profit_timeouts_and_stops(monkeypatch):
    """+2% take (covers the 1.2% round-trip cost), -10% stop, 5-minute limit."""
    from solana_launch_guard.momentum_take_shadow import momentum_take_policy
    monkeypatch.setenv("MOMENTUM_TAKE_PCT", "2")
    monkeypatch.setenv("MOMENTUM_TAKE_STOP_LOSS_PCT", "10")
    monkeypatch.setenv("MOMENTUM_TAKE_MAX_HOLD_SECONDS", "300")
    policy = momentum_take_policy()

    def state(price, age_seconds, peak=None):
        pos = position(opened_at=time.time() - age_seconds,
                       highest_price_since_entry=peak or max(price, 1.0))
        review = assess_exit(pos, {**QUOTE, "price": price}, policy)
        return review["state"], " ".join(review["reasons"])

    take, why = state(1.021, 60)
    assert take == "EXIT" and "+2% target" in why          # small profit, sold fast
    assert state(1.015, 60)[0] != "EXIT"                   # under target, still young
    stop, why = state(0.89, 60)
    assert stop == "EXIT" and "hard stop" in why           # -10% cut early, not -20%
    stale, why = state(0.99, 330)
    assert stale == "EXIT" and "max hold" in why           # 5-minute limit
    assert state(0.99, 120)[0] != "EXIT"                   # not yet 5 minutes
    # the old way: a flat coin with positive momentum was held past the window
    still = assess_exit(position(opened_at=time.time() - 330),
                        {**QUOTE, "price": 1.0}, ShadowRecoveryPolicy())
    assert still["state"] != "EXIT" or "max hold" not in " ".join(still["reasons"])

