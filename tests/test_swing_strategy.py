"""swing-v1: paper-only multi-hour sibling of hunter-v1 (2026-09-27)."""
import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from solana_launch_guard.eval_cli import main as eval_main
from solana_launch_guard.evaluation import (
    CostModel,
    LadderRules,
    Observation,
    TrackedDecision,
    simulate_ladder_trade,
)
from solana_launch_guard.hunter_shadow_strategy import ShadowRecoveryPolicy, assess_exit
from solana_launch_guard.swing_strategy import (
    SWING_AGENT_ID,
    SwingCapitalBook,
    SwingSettings,
    review_swing_exit,
    swing_cycle,
)

from test_hunter_shadow_strategy import candidate

S = SwingSettings()
POLICY = ShadowRecoveryPolicy()
DAY_MS = 86_400_000


def _position(entry=1.0, peak=None, hours_ago=1.0):
    opened = datetime.now(UTC) - timedelta(hours=hours_ago)
    return {"entry_price": entry, "highest_price_since_entry": peak or entry,
            "opened_at": opened.isoformat(), "entry_liquidity_usd": 80_000,
            "principal_recovered": False, "principal_secured": False}


def _quote(price, **flow):
    return {"price": price, "liquidity_usd": 80_000, "price_change_m5_pct": -1.0,
            "buys_m5": 10, "sells_m5": 10, **flow}


def test_ladder_preset_has_no_stagnation_and_multi_hour_limits():
    rules = S.ladder_rules(LadderRules())
    assert rules.stagnation_enabled is False
    assert rules.stop_loss_pct == 35 and rules.max_hold_seconds == 24 * 3600
    assert rules.trailing_activation_pct == 50 and rules.trailing_stop_pct == 25


def test_a_quiet_hour_is_not_an_exit():
    position, quote = _position(hours_ago=1), _quote(1.0)
    # hunter-v1 cuts this after 5 minutes...
    assert assess_exit(position, quote, POLICY)["state"] == "EXIT"
    # ...swing-v1 keeps holding.
    review = review_swing_exit(position, quote, S.exit_policy(POLICY), S,
                               now=time.time())
    assert review["state"] == "HOLD"


@pytest.mark.parametrize("price, state", [(0.75, "HOLD"), (0.64, "EXIT")])
def test_wider_hard_stop(price, state):
    review = review_swing_exit(_position(), _quote(price), S.exit_policy(POLICY), S,
                               now=time.time())
    assert review["state"] == state


def test_max_hold_sells():
    review = review_swing_exit(_position(hours_ago=25), _quote(1.1),
                               S.exit_policy(POLICY), S, now=time.time())
    assert review["state"] == "EXIT" and "max hold" in review["reasons"][-1]


def test_price_only_trailing_stop_off_the_board():
    position = _position(peak=1.8)            # peaked +80%
    no_flow = {"price": 1.3, "liquidity_usd": 80_000}  # -28% from peak
    review = review_swing_exit(position, no_flow, S.exit_policy(POLICY), S,
                               now=time.time())
    assert review["state"] == "EXIT" and "price alone" in review["reasons"][-1]
    small_dip = {"price": 1.6, "liquidity_usd": 80_000}
    assert review_swing_exit(position, small_dip, S.exit_policy(POLICY), S,
                             now=time.time())["state"] != "EXIT"


def test_simulator_holds_through_the_quiet_start_and_catches_the_move():
    """The PAID/fone shape: flat for hours, then a big move."""
    t0 = 1_000_000.0
    path = [Observation(t0 + i * 60, 1.0, 80_000, True) for i in range(60)]
    path += [Observation(t0 + 3600 + i * 900, p, 80_000, True)
             for i, p in enumerate([1.0, 0.9, 1.2, 1.8, 2.6, 3.0, 2.2])]
    decision = TrackedDecision("signal", "M", t0, "BUY ZONE", (), tuple(path))
    costs = CostModel(slippage_bps_per_side=60, fee_bps_per_side=0)
    ladder = simulate_ladder_trade(decision, LadderRules(), costs)
    swing = simulate_ladder_trade(decision, S.ladder_rules(LadderRules()), costs)
    assert ladder is not None and swing is not None
    assert ladder.held_seconds < 900           # stagnation after ~5 minutes
    assert swing.pnl_usd > 2 > ladder.pnl_usd


def _snapshot(tmp_path, rows):
    now = time.time()
    path = tmp_path / "board.json"
    for row in rows:
        row["quoted_at"] = now
    path.write_text(json.dumps({"generated_at": now, "candidates": rows}))
    return json.loads(path.read_text())


def _book(tmp_path):
    book = SwingCapitalBook(tmp_path / "swing.json")
    book.initialize(30)
    return book


def _run(book, snapshot, quotes=None):
    async def fetch(mints):
        return {m: q for m, q in (quotes or {}).items() if m in mints}

    return asyncio.run(swing_cycle(book, snapshot, settings=S, policy=POLICY,
                                   fetch_quotes=fetch))


def test_cycle_buys_the_best_buy_ready_candidate_under_the_live_gate(tmp_path,
                                                                      monkeypatch):
    monkeypatch.setenv("ENTRY_ALLOWED_DECISIONS", "BUY ZONE")
    monkeypatch.setenv("ENTRY_MIN_TOKEN_AGE_DAYS", "3")
    old = time.time() * 1000 - 10 * DAY_MS
    rows = [
        candidate(mint="A" * 44, signal_score=70, pair_created_at_ms=old),
        candidate(mint="B" * 44, signal_score=90, pair_created_at_ms=old),
        candidate(mint="C" * 44, signal_score=99, pair_created_at_ms=old,
                  decision="MOMENTUM BUY"),                      # gated like live
        candidate(mint="D" * 44, signal_score=95,
                  pair_created_at_ms=time.time() * 1000 - DAY_MS),  # too young
    ]
    book = _book(tmp_path)
    result = _run(book, _snapshot(tmp_path, rows))
    assert result["entry"]["mint"] == "B" * 44
    assert list(book.load()["agents"][SWING_AGENT_ID]["positions"]) == ["B" * 44]


def test_off_board_positions_are_priced_directly_and_stopped(tmp_path):
    book = _book(tmp_path)
    mint = "E" * 44
    book.reserve_shadow_buy(agent_id=SWING_AGENT_ID, mint=mint, symbol="E",
                            amount_usd=5, entry_price=1.0, price_currency="USD")
    result = _run(book, _snapshot(tmp_path, []),
                  quotes={mint: {"price": 0.6, "price_currency": "USD",
                                 "liquidity_usd": 50_000}})
    [review] = result["exits"]
    assert review["state"] == "EXIT" and "hard stop" in " ".join(review["reasons"])
    assert book.load()["agents"][SWING_AGENT_ID]["positions"] == {}
    fill = book.load()["agents"][SWING_AGENT_ID]["completed_trades"][0]
    assert fill["slippage_pct"] == S.round_trip_cost_pct


def test_eval_report_accepts_the_swing_model(tmp_path, capsys):
    eval_main(["--outcomes-db", str(tmp_path / "o.db"), "report",
               "--exit-model", "swing", "--launch-db", str(tmp_path / "none.db")])
    assert capsys.readouterr().out


# --- averaging down -------------------------------------------------------

def _path(t0, prices, step=900):
    first = [Observation(t0 + i * 60, 1.0, 80_000, True) for i in range(5)]
    return first + [Observation(t0 + 300 + i * step, p, 80_000, True)
                    for i, p in enumerate(prices)]


def _sim(prices, max_adds):
    t0 = 2_000_000.0
    decision = TrackedDecision("signal", "M", t0, "BUY ZONE", (),
                               tuple(_path(t0, prices)))
    rules = SwingSettings(max_adds=max_adds).ladder_rules(LadderRules())
    return simulate_ladder_trade(
        decision, rules, CostModel(slippage_bps_per_side=60, fee_bps_per_side=0,
                                   fixed_fee_usd_per_side=0))


def test_adds_help_when_the_dip_recovers():
    prices = [0.78, 0.62, 0.9, 1.05, 1.1]
    with_adds, without = _sim(prices, 2), _sim(prices, 0)
    assert with_adds.exit_reason.startswith("ADD")
    assert with_adds.pnl_usd > 0 > without.pnl_usd


def test_adds_cost_more_when_it_keeps_falling():
    prices = [0.78, 0.62, 0.45, 0.3]
    with_adds, without = _sim(prices, 2), _sim(prices, 0)
    assert "ADD" in with_adds.exit_reason and "STOP_LOSS" in with_adds.exit_reason
    assert with_adds.pnl_usd < without.pnl_usd < 0   # the honest downside


def _held(tmp_path, *, liquidity=80_000):
    book = _book(tmp_path)
    mint = "F" * 44
    book.reserve_shadow_buy(agent_id=SWING_AGENT_ID, mint=mint, symbol="F",
                            amount_usd=5, entry_price=1.0, price_currency="USD",
                            entry_liquidity_usd=80_000)
    return book, mint


def _off_board(price, liquidity=80_000):
    return {"price": price, "price_currency": "USD", "liquidity_usd": liquidity}


def test_paper_adds_twice_at_most_and_lowers_the_average(tmp_path):
    book, mint = _held(tmp_path)
    [review] = _run(book, _snapshot(tmp_path, []), {mint: _off_board(0.78)})["exits"]
    assert review["state"] == "ADD"
    position = book.load()["agents"][SWING_AGENT_ID]["positions"][mint]
    assert position["adds"] == 1 and position["allocated_usd"] == 7.5
    assert position["entry_price"] == pytest.approx(7.5 / (5 + 2.5 / 0.78))
    assert book.load()["agents"][SWING_AGENT_ID]["cash_usd"] == 22.5
    _run(book, _snapshot(tmp_path, []), {mint: _off_board(0.70)})   # -23% vs avg
    _run(book, _snapshot(tmp_path, []), {mint: _off_board(0.60)})   # would be a 3rd
    position = book.load()["agents"][SWING_AGENT_ID]["positions"][mint]
    assert position["adds"] == 2 and position["allocated_usd"] == 10.0


def test_no_add_into_a_draining_pool(tmp_path):
    book, mint = _held(tmp_path)
    [review] = _run(book, _snapshot(tmp_path, []),
                    {mint: _off_board(0.78, liquidity=40_000)})["exits"]
    assert review["state"] == "HOLD"
    assert "adds" not in book.load()["agents"][SWING_AGENT_ID]["positions"][mint]


def test_stop_is_measured_from_the_average_cost(tmp_path):
    book, mint = _held(tmp_path)
    _run(book, _snapshot(tmp_path, []), {mint: _off_board(0.78)})   # avg ~0.914
    # 0.62 is -38% from the first price but only -32% from the average: held.
    [review] = _run(book, _snapshot(tmp_path, []), {mint: _off_board(0.62)})["exits"]
    assert review["state"] == "ADD"
