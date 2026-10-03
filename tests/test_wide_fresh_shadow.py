"""wide-fresh-v1: wide-v1 plus a fresh-setup re-entry gate (2026-10-01).

Found in wide-v1's first 50 trades: 28 of 30 closed trades were the same
token (HYPE), bought and stagnation-exited every time it chopped back into
its own ~1.5%-wide range. The shared re-entry rule only requires price to
reclaim the entry of a losing streak, which a narrow range clears in
minutes. This gate requires a genuine new high above the peak of the
previous position on the same signal type before a mint can be bought on it
again.
"""
import asyncio
import json
import time

from solana_launch_guard.hunter_shadow_strategy import ShadowRecoveryPolicy
from solana_launch_guard.wide_fresh_shadow import (
    WIDE_FRESH_AGENT_ID,
    WideFreshCapitalBook,
    fresh_setup_reason,
    wide_fresh_cycle,
)

from test_hunter_shadow_strategy import candidate

POLICY = ShadowRecoveryPolicy()


def _snapshot(rows):
    now = time.time()
    for row in rows:
        row["quoted_at"] = now
    return json.loads(json.dumps({"generated_at": now, "candidates": rows}))


def _book(tmp_path):
    book = WideFreshCapitalBook(tmp_path / "wide_fresh.json")
    book.initialize(30)
    return book


def _run(book, snapshot, quotes=None, **kwargs):
    async def fetch(mints):
        return {m: q for m, q in (quotes or {}).items() if m in mints}

    return asyncio.run(wide_fresh_cycle(book, snapshot, policy=POLICY,
                                        fetch_quotes=fetch, **kwargs))


# --- fresh_setup_reason, in isolation ---------------------------------------

def test_a_mint_never_closed_before_is_unrestricted():
    assert fresh_setup_reason({}, "M" * 44, "EARLY BUY", candidate()) is None


def test_a_different_decision_type_is_unrestricted():
    gate = {"M" * 44: {"decision": "BUY ZONE", "peak_price": 1.0}}
    assert fresh_setup_reason(
        gate, "M" * 44, "EARLY BUY", candidate(peak_price=0.95)) is None


def test_chopping_back_into_the_old_range_is_blocked():
    gate = {"M" * 44: {"decision": "EARLY BUY", "peak_price": 1.0}}
    reason = fresh_setup_reason(
        gate, "M" * 44, "EARLY BUY", candidate(peak_price=0.98, price=0.95))
    assert reason is not None and "no new high above 1" in reason


def test_a_genuine_new_high_clears_the_gate():
    gate = {"M" * 44: {"decision": "EARLY BUY", "peak_price": 1.0}}
    assert fresh_setup_reason(
        gate, "M" * 44, "EARLY BUY", candidate(peak_price=1.15, price=1.05)) is None


def test_no_prior_peak_recorded_is_unrestricted():
    gate = {"M" * 44: {"decision": "EARLY BUY", "peak_price": 0.0}}
    assert fresh_setup_reason(gate, "M" * 44, "EARLY BUY", candidate()) is None


# --- wide_fresh_cycle, end to end --------------------------------------------

def test_the_hype_pattern_is_blocked_after_one_round_trip(tmp_path):
    """The exact shape of what wide-v1 actually did: buy, get stagnation-
    stopped while still on the board (so the board's own peak tracker is
    available), and try to buy again inside the same range the board's own
    peak_price shows never broke. wide-fresh-v1 must refuse the second buy."""
    mint = "H" * 44
    book = _book(tmp_path)
    bought = _run(book, _snapshot([
        candidate(mint=mint, decision="EARLY BUY", price=0.9, peak_price=1.0)]))
    assert bought["entry"] is not None
    # Still on the board: the board's own peak (1.0) never moved while the
    # price dropped enough to hit the hard stop.
    _run(book, _snapshot([
        candidate(mint=mint, decision="EARLY BUY", price=0.6, peak_price=1.0)]))
    assert mint not in book.load()["agents"][WIDE_FRESH_AGENT_ID]["positions"]
    # Offered the same setup again, with the board's peak still at 1.0.
    blocked = _run(book, _snapshot([
        candidate(mint=mint, decision="EARLY BUY", price=0.92, peak_price=0.98)]))
    assert blocked["entry"] is None


def test_the_block_survives_a_process_restart(tmp_path):
    """fresh_gate is read from and written to disk on every call - nothing
    is held in memory between cycles - so a second WideFreshCapitalBook
    instance pointed at the same file (standing in for a fresh `launch-guard
    -wide-fresh` process after a restart) must see the same gate state and
    refuse the same re-buy, not reset because the process did."""
    mint = "H" * 44
    path = tmp_path / "wide_fresh.json"
    first_process = WideFreshCapitalBook(path)
    first_process.initialize(30)
    _run(first_process, _snapshot([
        candidate(mint=mint, decision="EARLY BUY", price=0.9, peak_price=1.0)]))
    _run(first_process, _snapshot([
        candidate(mint=mint, decision="EARLY BUY", price=0.6, peak_price=1.0)]))
    assert mint not in first_process.load()["agents"][WIDE_FRESH_AGENT_ID]["positions"]

    # A brand new book object, as `main()` would construct on the next run -
    # no state shared with `first_process` except the file on disk.
    restarted_process = WideFreshCapitalBook(path)
    blocked = _run(restarted_process, _snapshot([
        candidate(mint=mint, decision="EARLY BUY", price=0.92, peak_price=0.98)]))
    assert blocked["entry"] is None

    # And it still recognizes a genuine new high after the "restart".
    allowed = _run(restarted_process, _snapshot([
        candidate(mint=mint, decision="EARLY BUY", price=1.15, peak_price=1.3)]))
    assert allowed["entry"] is not None and allowed["entry"]["mint"] == mint


def test_a_real_new_high_lets_it_back_in(tmp_path):
    mint = "H" * 44
    book = _book(tmp_path)
    _run(book, _snapshot([
        candidate(mint=mint, decision="EARLY BUY", price=0.9, peak_price=1.0)]))
    _run(book, _snapshot([
        candidate(mint=mint, decision="EARLY BUY", price=0.6, peak_price=1.0)]))
    # Now the board's own peak tracker shows a real breakout above 1.0, and
    # price has pulled back into a fresh, qualifying zone above that high.
    allowed = _run(book, _snapshot([
        candidate(mint=mint, decision="EARLY BUY", price=1.15, peak_price=1.3)]))
    assert allowed["entry"] is not None and allowed["entry"]["mint"] == mint


def test_a_different_signal_type_on_the_same_mint_is_not_gated(tmp_path):
    mint = "H" * 44
    book = _book(tmp_path)
    _run(book, _snapshot([
        candidate(mint=mint, decision="EARLY BUY", price=0.9, peak_price=1.0)]))
    _run(book, _snapshot([
        candidate(mint=mint, decision="EARLY BUY", price=0.6, peak_price=1.0)]))
    allowed = _run(book, _snapshot([
        candidate(mint=mint, decision="BUY ZONE", price=0.92, peak_price=0.98)]))
    assert allowed["entry"] is not None and allowed["entry"]["mint"] == mint


def test_exits_still_use_hunters_rules_and_the_same_cost(tmp_path):
    book = _book(tmp_path)
    _run(book, _snapshot([candidate(mint="E" * 44)]))
    result = _run(book, _snapshot([]), quotes={
        "E" * 44: {"price": 0.6, "price_currency": "USD", "liquidity_usd": 20_000}})
    [review] = result["exits"]
    assert review["state"] == "EXIT" and "hard stop" in " ".join(review["reasons"])
    fill = book.load()["agents"][WIDE_FRESH_AGENT_ID]["completed_trades"][0]
    assert fill["slippage_pct"] == 1.2


def test_gate_state_prefers_the_boards_own_peak_over_the_positions(tmp_path):
    """A quick stop-out can close with highest_price_since_entry no higher
    than its own entry price. The gate must use the board's own (longer-
    window) peak when a fresh quote carries one, or any later candidate
    near the entry price would misread as a genuine new high."""
    mint = "H" * 44
    book = _book(tmp_path)
    _run(book, _snapshot([
        candidate(mint=mint, decision="EARLY BUY", price=0.9, peak_price=1.0)]))
    _run(book, _snapshot([
        candidate(mint=mint, decision="EARLY BUY", price=0.6, peak_price=1.0)]))
    gate = book.load()["agents"][WIDE_FRESH_AGENT_ID]["fresh_gate"][mint]
    assert gate["decision"] == "EARLY BUY"
    assert gate["peak_price"] == 1.0  # the board's peak, not the 0.9 entry


def test_gate_state_falls_back_to_the_positions_own_high_off_board(tmp_path):
    """Off-board, the exit quote carries no peak_price field at all (unlike
    a board candidate), so the gate must fall back to the position's own
    highest_price_since_entry rather than crash or skip recording."""
    mint = "H" * 44
    book = _book(tmp_path)
    _run(book, _snapshot([
        candidate(mint=mint, decision="EARLY BUY", price=0.9, peak_price=1.0)]))
    _run(book, _snapshot([]), quotes={
        mint: {"price": 0.6, "price_currency": "USD", "liquidity_usd": 20_000}})
    gate = book.load()["agents"][WIDE_FRESH_AGENT_ID]["fresh_gate"][mint]
    assert gate["decision"] == "EARLY BUY"
    assert gate["peak_price"] == 0.9  # entry price: never marked higher off-board


def test_four_slots_then_full(tmp_path):
    book = _book(tmp_path)
    for i in range(5):
        _run(book, _snapshot([candidate(mint=str(i) * 44, signal_score=90)]))
    assert len(book.load()["agents"][WIDE_FRESH_AGENT_ID]["positions"]) == 4


def test_min_token_age_skips_minutes_old_launches(tmp_path):
    now = time.time()
    young = candidate(mint="Y" * 44, decision="EARLY BUY",
                      pair_created_at_ms=(now - 10 * 60) * 1000)
    assert _run(_book(tmp_path), _snapshot([young]),
                min_token_age_minutes=45)["entry"] is None
    other = WideFreshCapitalBook(tmp_path / "other.json")
    other.initialize(30)
    assert _run(other, _snapshot([dict(young)]))["entry"] is not None  # guard off


# --- rug block: a collapsed token is dead for the book, new high or not ------

def test_a_rugged_mint_is_blocked_on_any_signal_until_the_block_expires():
    now = time.time()
    gate = {"M" * 44: {"decision": "EARLY BUY", "peak_price": 1.0,
                       "rugged_until": now + 3600}}
    new_high = candidate(peak_price=1.5, price=1.4)
    reason = fresh_setup_reason(gate, "M" * 44, "BUY ZONE", new_high, now=now)
    assert reason is not None and reason.startswith("rugged")
    assert fresh_setup_reason(gate, "M" * 44, "EARLY BUY", new_high,
                              now=now + 7200) is None  # expired: new high allowed


def _rug_then_pump(tmp_path, **cycle):
    """Bought at 0.9, collapses to 0.6 (-33%), then pumps to a new high."""
    mint = "H" * 44
    book = _book(tmp_path)
    _run(book, _snapshot([
        candidate(mint=mint, decision="EARLY BUY", price=0.9,
                  peak_price=1.0)]), **cycle)
    _run(book, _snapshot([
        candidate(mint=mint, decision="EARLY BUY", price=0.6,
                  peak_price=1.0)]), **cycle)
    assert mint not in book.load()["agents"][WIDE_FRESH_AGENT_ID]["positions"]
    return _run(book, _snapshot([
        candidate(mint=mint, decision="EARLY BUY", price=1.15,
                  peak_price=1.3)]), **cycle)


def test_without_the_rug_block_a_pump_to_a_new_high_is_bought_again(tmp_path):
    assert _rug_then_pump(tmp_path)["entry"] is not None  # the GT2gF3 pattern


def test_with_the_rug_block_the_same_pump_is_not_bought(tmp_path):
    again = _rug_then_pump(tmp_path, rug_block_hours=24, rug_loss_pct=30)
    assert again["entry"] is None


def test_a_small_loss_is_not_a_rug(tmp_path):
    mint = "S" * 44
    book = _book(tmp_path)
    kw = dict(rug_block_hours=24, rug_loss_pct=30)
    _run(book, _snapshot([
        candidate(mint=mint, decision="EARLY BUY", price=0.9, peak_price=1.0)]), **kw)
    # -20% hard stop: a loss, but under the 30% rug line
    _run(book, _snapshot([
        candidate(mint=mint, decision="EARLY BUY", price=0.71, peak_price=1.0)]), **kw)
    gate = book.load()["agents"][WIDE_FRESH_AGENT_ID]["fresh_gate"][mint]
    assert "rugged_until" not in gate


# --- bought young: not again until the token is 2 hours old ------------------

def _bought_young_then_closed(tmp_path, *, age_minutes, **cycle):
    """Buy a token that is `age_minutes` old, then stop it out."""
    mint = "Y" * 44
    book = _book(tmp_path)
    created = (time.time() - age_minutes * 60) * 1000
    _run(book, _snapshot([candidate(
        mint=mint, decision="EARLY BUY", price=0.9, peak_price=1.0,
        pair_created_at_ms=created)]), **cycle)
    _run(book, _snapshot([candidate(
        mint=mint, decision="EARLY BUY", price=0.7, peak_price=1.0,
        pair_created_at_ms=created)]), **cycle)
    assert mint not in book.load()["agents"][WIDE_FRESH_AGENT_ID]["positions"]
    return book, mint, created


RULE = dict(young_buy_minutes=60, young_rebuy_after_minutes=120)


def _offer_later(book, mint, created_ms, *, token_age_minutes):
    """The same token, new high, offered when it is `token_age_minutes` old
    (its creation time is fixed, so we move the quote's clock by re-basing)."""
    shift = (time.time() - token_age_minutes * 60) * 1000 - created_ms
    return _run(book, _snapshot([candidate(
        mint=mint, decision="EARLY BUY", price=1.15, peak_price=1.3,
        pair_created_at_ms=created_ms + shift)]), **RULE)


def test_a_token_bought_under_an_hour_old_waits_until_it_is_two_hours_old(tmp_path):
    book, mint, created = _bought_young_then_closed(tmp_path, age_minutes=10, **RULE)
    gate = book.load()["agents"][WIDE_FRESH_AGENT_ID]["fresh_gate"][mint]
    assert gate["rebuy_min_age_minutes"] == 120
    assert _offer_later(book, mint, created, token_age_minutes=90)["entry"] is None
    assert _offer_later(book, mint, created, token_age_minutes=130)["entry"] is not None


def test_a_token_bought_after_the_one_hour_mark_is_not_held_back(tmp_path):
    book, mint, created = _bought_young_then_closed(tmp_path, age_minutes=75, **RULE)
    gate = book.load()["agents"][WIDE_FRESH_AGENT_ID]["fresh_gate"][mint]
    assert "rebuy_min_age_minutes" not in gate


def test_the_young_rebuy_rule_is_off_unless_configured(tmp_path):
    book, mint, created = _bought_young_then_closed(tmp_path, age_minutes=10)
    gate = book.load()["agents"][WIDE_FRESH_AGENT_ID]["fresh_gate"][mint]
    assert "rebuy_min_age_minutes" not in gate


def test_an_unknown_token_age_cannot_prove_it_is_old_enough():
    gate = {"M" * 44: {"decision": "EARLY BUY", "peak_price": 1.0,
                       "rebuy_min_age_minutes": 120}}
    reason = fresh_setup_reason(gate, "M" * 44, "EARLY BUY",
                                candidate(peak_price=1.5, price=1.4), now=time.time())
    assert reason is not None and "young" in reason

