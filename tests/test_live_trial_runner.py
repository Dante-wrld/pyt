"""Only decision and submission guards; this module cannot broadcast."""
import asyncio
import time
from types import SimpleNamespace
import pytest

from solana_launch_guard.live_trial_ledger import LiveTrialLedger, TrialHalted
from solana_launch_guard.live_trial_runner import (
    REGROWTH_CONFIRMATION_POLLS, REGROWTH_MIN_GROWTH_PCT, REGROWTH_MIN_LIQUIDITY_USD, LiveEntryDecision,
    _live_arbiter, _mint_round_trip_history, _regrowth_bar_clears, can_submit,
    decide_hunter_entry, decide_regrowth_rebuy, execute_hunter_entry, execute_live_exit,
)
from solana_launch_guard.execution import USDC_MINT
from solana_launch_guard.market import MarketQuote


MINT = "A" * 44  # synthetic Base58-looking test mint


def _no_legacy_claim() -> SimpleNamespace:
    """A store.connection stub reporting no pre-existing legacy-key claim."""
    return SimpleNamespace(execute=lambda *a, **k: SimpleNamespace(fetchone=lambda: None))


def snapshot(*, confirmations=3, momentum="RISING", liquidity=60000,
             price=.005, planned_target_price=None, decision="BUY NOW"):
    now = time.time()
    return {"generated_at": now, "candidates": [{
        "chain": "solana", "mint": MINT, "symbol": "TEST",
        "quoted_at": now, "price": price, "peak_price": .006,
        "planned_target_price": planned_target_price,
        "pullback_from_peak_pct": 16, "liquidity_usd": liquidity,
        "initial_liquidity_usd": liquidity,
        "entry_confirmation_count": confirmations, "entry_confirmation_required": 3,
        "momentum_label": momentum, "price_change_m5_pct": 2,
        "volume_label": "RISING", "buys_m5": 20, "sells_m5": 10,
        "buy_sell_ratio": 2, "risk_label": "MEDIUM", "signal_score": 90,
        "decision": decision,
    }]}


class Model:
    def __init__(self, *, requested=5):
        self.calls = 0
        self.requested = requested

    def propose(self, *, role, context):
        self.calls += 1
        return {"action": "BUY", "mint": MINT, "requested_usd": self.requested,
                "confidence": .8, "thesis": "confirmed recovery", "evidence": [],
                "leader_wallet": None}


def test_recovery_model_and_arbiter_gate_before_reservation(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    model = Model(requested=5)
    assert decide_hunter_entry(snapshot(confirmations=0), model=model, ledger=book) is None
    assert decide_hunter_entry(snapshot(momentum="FALLING"), model=model, ledger=book) is None
    assert decide_hunter_entry(snapshot(liquidity=4000), model=model, ledger=book) is None
    assert model.calls == 0
    decision = decide_hunter_entry(snapshot(), model=model, ledger=book)
    assert decision.approved_cents == 500
    assert model.calls == 1
    book.reserve_buy(intent="buy", agent="hunter-v1", mint=MINT,
                     requested_cents=decision.requested_cents,
                     approved_cents=decision.approved_cents)
    assert can_submit(book, mint=MINT, snapshot=snapshot())
    assert not can_submit(book, mint=MINT, snapshot=snapshot(confirmations=0))
    with pytest.raises(TrialHalted, match="unresolved"):
        decide_hunter_entry(snapshot(), model=model, ledger=book)
    book.close()


def test_post_model_recheck_uses_a_fresh_read_not_the_aging_original(tmp_path, monkeypatch):
    """The model call reliably takes real wall-clock time (observed ~11-13s
    in production). Re-checking the *original* snapshot's age against a
    later time.time() fails purely from that latency, even though a
    genuinely fresh read on disk shows the candidate is still perfectly
    valid - decide_hunter_entry must re-fetch via current_snapshot instead
    of re-timestamping the same stale data it started with."""
    base = time.time()
    stale_snapshot = snapshot()
    stale_snapshot["generated_at"] = base - 5
    for c in stale_snapshot["candidates"]:
        c["quoted_at"] = base - 5

    fresh_snapshot = snapshot()
    fresh_snapshot["generated_at"] = base + 12
    for c in fresh_snapshot["candidates"]:
        c["quoted_at"] = base + 12

    # Only the internal recheck (after model.propose()) calls time.time()
    # with no `now` override - simulate ~12s of real elapsed model latency.
    monkeypatch.setattr("solana_launch_guard.live_trial_runner.time.time", lambda: base + 12)
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    model = Model()
    decision = decide_hunter_entry(
        stale_snapshot, model=model, ledger=book, now=base,
        current_snapshot=lambda: fresh_snapshot,
    )
    assert decision is not None
    assert decision.approved_cents == 500
    book.close()


def test_post_model_recheck_still_blocks_when_the_fresh_read_no_longer_qualifies(tmp_path, monkeypatch):
    base = time.time()
    stale_snapshot = snapshot()
    stale_snapshot["generated_at"] = base - 5
    for c in stale_snapshot["candidates"]:
        c["quoted_at"] = base - 5

    # The fresh read exists (not itself stale) but the candidate has moved on.
    fresh_snapshot = snapshot(momentum="FALLING")
    fresh_snapshot["generated_at"] = base + 12
    for c in fresh_snapshot["candidates"]:
        c["quoted_at"] = base + 12

    monkeypatch.setattr("solana_launch_guard.live_trial_runner.time.time", lambda: base + 12)
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    model = Model()
    decision = decide_hunter_entry(
        stale_snapshot, model=model, ledger=book, now=base,
        current_snapshot=lambda: fresh_snapshot,
    )
    assert decision is None
    skipped = [d for d in book.status()["recent_decisions"] if d["state"] == "BLOCKED"]
    assert skipped and "stale" in skipped[0]["reason"]
    book.close()


def test_live_arbiter_daily_loss_cap_absorbs_more_than_one_bad_trade():
    """3% of $30 equity ($0.90) was smaller than a single real stop-loss
    observed live (28-34% drawdowns on a $5 position, ~$1.40-1.75) - one
    bad trade always exhausted the whole day's allowance."""
    policy = _live_arbiter().policy
    assert policy.max_daily_loss_pct == 10
    assert 30 * policy.max_daily_loss_pct / 100 >= 3.0


def test_chase_abandons_once_price_reaches_the_frozen_original_target(tmp_path, monkeypatch):
    """Retrying a candidate that's already blown past the target we
    originally planned to exit at means buying at what would have been
    our own take-profit level - abandon instead of chasing forever."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    model = Model()
    chase_first_target: dict[str, float] = {}

    below_target = snapshot(price=0.005, planned_target_price=0.007)
    decision = decide_hunter_entry(
        below_target, model=model, ledger=book, chase_first_target=chase_first_target,
    )
    assert decision is not None
    assert chase_first_target[MINT] == 0.007
    assert model.calls == 1

    past_target = snapshot(price=0.008, planned_target_price=0.009)
    decision2 = decide_hunter_entry(
        past_target, model=model, ledger=book, chase_first_target=chase_first_target,
    )
    assert decision2 is None
    # The model must not even be called once price has already passed the
    # frozen target - there's no proposal worth asking for.
    assert model.calls == 1
    blocked = [d for d in book.status()["recent_decisions"] if d["state"] == "BLOCKED"]
    assert blocked and "abandoning the chase" in blocked[0]["reason"]
    book.close()


def test_chase_keeps_retrying_against_the_frozen_target_even_if_it_reanchors_higher(tmp_path, monkeypatch):
    """The recommendation engine re-anchors planned_target_price to a new
    peak whenever price makes a fresh high - the frozen (first-seen) value
    must be what's compared, or a genuinely still-climbing candidate would
    never trigger the abandon check because the live target always chases
    the price up with it."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    model = Model()
    chase_first_target: dict[str, float] = {}

    first = snapshot(price=0.005, planned_target_price=0.007)
    decide_hunter_entry(first, model=model, ledger=book, chase_first_target=chase_first_target)
    assert chase_first_target[MINT] == 0.007

    # Price and the live target both rose, but price (0.0065) is still
    # below the *frozen* original target (0.007) - must keep retrying.
    still_climbing = snapshot(price=0.0065, planned_target_price=0.012)
    decision = decide_hunter_entry(
        still_climbing, model=model, ledger=book, chase_first_target=chase_first_target,
    )
    assert decision is not None
    assert chase_first_target[MINT] == 0.007  # unchanged, still frozen
    book.close()


def test_liquidity_floor_follows_policy_not_a_hardcoded_fifty_thousand(tmp_path, monkeypatch):
    """_fresh_candidates and _live_arbiter used to hardcode a $50,000
    liquidity floor, completely independent of INTELLIGENCE_MIN_LIQUIDITY_USD
    - a candidate above the configured policy floor but below $50,000 (a real
    case: MAZE at ~$32,900 liquidity, confirmed MOMENTUM BUY, never even
    reached assess_entry) was silently dropped before evaluation, with no
    ledger entry at all. The floor must track policy.min_liquidity_usd."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    monkeypatch.setenv("INTELLIGENCE_MIN_LIQUIDITY_USD", "10000")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    model = Model(requested=5)
    decision = decide_hunter_entry(snapshot(liquidity=32_900), model=model, ledger=book)
    assert model.calls == 1
    assert decision is not None
    assert decision.approved_cents == 500
    book.close()


def test_buy_zone_candidate_that_fails_the_stricter_entry_policy_is_logged(tmp_path, monkeypatch):
    """A candidate can already show BUY ZONE/BUY NOW in the recommendation
    feed (and so have already sent a phone alert) while still failing
    decide_hunter_entry's stricter, independent policy. That gap must not
    vanish silently - it needs a ledger entry explaining why, or
    --live-trial-status can't answer "why didn't the trial buy that?".
    """
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    assert decide_hunter_entry(snapshot(confirmations=0), model=Model(), ledger=book) is None
    skipped = [d for d in book.status()["recent_decisions"] if d["state"] == "BUY_ZONE_SKIPPED"]
    assert len(skipped) == 1
    assert skipped[0]["mint"] == MINT
    assert "confirmations" in skipped[0]["reason"]
    book.close()


def test_model_cannot_increase_capital_or_buy_other_mint(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    assert decide_hunter_entry(snapshot(), model=Model(requested=8), ledger=book).approved_cents == 500
    class Other(Model):
        def propose(self, **kwargs):
            raw = super().propose(**kwargs)
            raw["mint"] = "B" * 44
            return raw
    assert decide_hunter_entry(snapshot(), model=Other(), ledger=book) is None
    book.close()


def _seed_sell(book, *, mint=MINT, realized_cents, at):
    """A synthetic completed sell order, as if a prior round trip on this
    mint already closed this session - only the fields
    _mint_round_trip_history actually reads."""
    book.db.execute(
        "INSERT INTO orders(intent,agent,side,mint,state,realized_cents,created_at) "
        "VALUES(?,?,'SELL',?,'CONFIRMED',?,?)",
        (f"seed:{mint}:{at}", "hunter-v1", mint, realized_cents, at),
    )
    book.db.commit()


def test_mint_round_trip_history_computes_consecutive_loss_streak(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    assert _mint_round_trip_history(book, MINT) == {
        "round_trips": 0, "total_realized_usd": 0.0, "consecutive_losses": 0,
    }
    _seed_sell(book, realized_cents=-50, at=1)
    _seed_sell(book, realized_cents=100, at=2)
    _seed_sell(book, realized_cents=-30, at=3)
    _seed_sell(book, realized_cents=-20, at=4)
    history = _mint_round_trip_history(book, MINT)
    assert history["round_trips"] == 4
    assert history["total_realized_usd"] == pytest.approx(0.0)
    # Only the trailing streak counts - the win at index 2 breaks it, even
    # though there's an earlier loss before it.
    assert history["consecutive_losses"] == 2
    book.close()


def test_decide_hunter_entry_blocks_a_mint_after_two_consecutive_losses(tmp_path, monkeypatch):
    """A mint that has already lost money twice in a row this session gets
    refused a fresh buy outright - the model never even gets a chance to
    re-litigate it, mirroring how emergency-exit is deterministic rather
    than trusting the model to notice its own trade history."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    _seed_sell(book, realized_cents=-40, at=1)
    _seed_sell(book, realized_cents=-60, at=2)
    model = Model()
    assert decide_hunter_entry(snapshot(), model=model, ledger=book) is None
    assert model.calls == 0
    book.close()


def test_decide_hunter_entry_still_allows_a_buy_after_one_loss(tmp_path, monkeypatch):
    """One prior loss on a mint is real evidence for the model to weigh,
    not grounds to refuse it outright - only a repeated pattern (two in a
    row) triggers the deterministic block."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    _seed_sell(book, realized_cents=-40, at=1)
    captured = {}
    class Capturing(Model):
        def propose(self, *, role, context):
            captured.update(context)
            return super().propose(role=role, context=context)
    decision = decide_hunter_entry(snapshot(), model=Capturing(), ledger=book)
    assert decision is not None
    assert captured["mint_trade_history"] == {
        "round_trips": 1, "total_realized_usd": -0.4, "consecutive_losses": 1,
    }
    book.close()


def test_decide_hunter_entry_skips_the_model_once_the_daily_loss_limit_is_breached(tmp_path, monkeypatch):
    """The daily loss limit (10% of the $30 equity used for live trading,
    i.e. -$3.00) is a hard deterministic gate that doesn't depend on
    anything the model would say. Checking it before the model call - not
    only inside the arbitration that follows it - saves a real OpenAI
    request every cycle once the limit is breached, instead of paying for
    a proposal that was always going to be blocked (observed live: the
    same BUY_READY candidate kept proposing and getting blocked every
    ~30s cycle after the daily loss cap was already exceeded)."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    _seed_sell(book, mint="B" * 44, realized_cents=-350, at=time.time())
    model = Model()
    assert decide_hunter_entry(snapshot(), model=model, ledger=book) is None
    assert model.calls == 0
    book.close()


def test_momentum_buy_is_paused_and_never_reaches_the_model(tmp_path, monkeypatch):
    """Paused 2026-09-23 after a full session showed 17 losses vs 1 win,
    concentrated almost entirely in MOMENTUM BUY entries - it must not
    originate a buy, or even call the model, while paused."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    model = Model()
    assert decide_hunter_entry(snapshot(decision="MOMENTUM BUY"), model=model, ledger=book) is None
    assert model.calls == 0
    assert book.status()["recent_decisions"][0]["state"] == "BUY_ZONE_SKIPPED"
    assert "paused" in book.status()["recent_decisions"][0]["reason"].lower()
    book.close()


def test_pullback_entries_still_run_while_momentum_buy_is_paused(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    model = Model()
    decision = decide_hunter_entry(snapshot(decision="BUY NOW"), model=model, ledger=book)
    assert decision is not None
    assert model.calls == 1
    book.close()


def test_momentum_buy_would_otherwise_qualify_confirming_the_pause_is_what_blocks_it(tmp_path, monkeypatch):
    """Proves MOMENTUM_BUY_PAUSED is the actual mechanism doing the
    blocking, not some unrelated snapshot() default - the same candidate
    is BUY_READY and gets bought once the pause is lifted."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    monkeypatch.setattr("solana_launch_guard.live_trial_runner.MOMENTUM_BUY_PAUSED", False)
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    model = Model()
    momentum_snapshot = snapshot(decision="MOMENTUM BUY")
    # MOMENTUM BUY's own evidence bar also requires a minimum five-minute
    # trade count, separate from confirmations - snapshot()'s default
    # buys_m5/sells_m5 (20/10) clears every other decision's bar but not
    # this one, so bump it to isolate the pause as what's actually tested.
    momentum_snapshot["candidates"][0].update(buys_m5=40, sells_m5=20)
    decision = decide_hunter_entry(momentum_snapshot, model=model, ledger=book)
    assert decision is not None
    assert model.calls == 1
    book.close()


def test_early_buy_entry_is_capped_at_half_the_normal_order_size(tmp_path, monkeypatch):
    """An early-buy entry has no proven move behind it yet, so it earns
    only half the normal $5 order size, even if the model requests more."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    decision = decide_hunter_entry(snapshot(decision="EARLY BUY"), model=Model(requested=5), ledger=book)
    assert decision is not None
    assert decision.approved_cents == 250
    book.close()


def test_a_second_buy_of_the_same_mint_after_a_full_round_trip_does_not_collide(tmp_path, monkeypatch):
    """A mint can legitimately be bought, fully sold, and re-qualify for a
    fresh buy later in the same session (observed live: WHT round-tripped
    and hunter-v1 re-entered it) - the second buy's intent key must not
    collide with the first, already-CONFIRMED order's primary key and
    crash the whole supervisor with a raw sqlite3.IntegrityError."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    wallet = "synthetic-owner"

    class Rpc:
        async def token_balance(self, owner, mint):
            return SimpleNamespace(raw_amount=20_000_000 if mint == USDC_MINT else 0)
        async def mint_decimals(self, mint):
            return 6
        async def get_transaction(self, signature):
            def row(index, mint, amount):
                return {"accountIndex": index, "mint": mint, "owner": wallet,
                        "uiTokenAmount": {"amount": str(amount)}}
            return {"meta": {"err": None,
                    "preTokenBalances": [row(0, USDC_MINT, 20_000_000)],
                    "postTokenBalances": [row(0, USDC_MINT, 15_000_000), row(1, MINT, 100_000_000)]}}

    class Client:
        async def order(self, **kwargs):
            return {"inputMint": MINT, "outputMint": USDC_MINT,
                    "inAmount": str(kwargs["amount_raw"]), "outAmount": "4100000",
                    "otherAmountThreshold": "4000000", "priceImpact": "-1",
                    "slippageBps": 250}

    class Buyer:
        client = Client()
        max_price_impact_pct = 3
        max_slippage_bps = 300
        async def preflight(self, intent, rpc):
            return SimpleNamespace(prepared=SimpleNamespace(input_amount_raw=intent.amount_usdc_raw,
                minimum_output_raw=100_000_000, expected_output_raw=100_000_000))
        async def execute(self, prepared):
            return SimpleNamespace(signature="public-signature", input_amount_raw=5_000_000,
                                   output_amount_raw=100_000_000)

    class Store:
        connection = _no_legacy_claim()
        def begin_auto_buy_execution(self, **kwargs):
            return True
        def complete_auto_buy_execution(self, **kwargs):
            pass
        def save_owned_holding(self, holding):
            pass
        def arm_auto_sell(self, mint, **kwargs):
            pass

    decision = decide_hunter_entry(snapshot(), model=Model(), ledger=book)
    receipt = asyncio.run(execute_hunter_entry(decision, ledger=book, rpc=Rpc(),
                          buyer=Buyer(), store=Store(), wallet=wallet,
                          current_snapshot=snapshot))
    assert receipt["spent_cents"] == 500
    # Simulate a completed sell closing the position out, same as a real
    # round trip would leave behind - only what reserve_buy's own checks
    # inspect (no open position, no unresolved BUY order for this mint).
    book.db.execute("DELETE FROM positions WHERE mint=?", (MINT,))
    book.db.commit()

    second = decide_hunter_entry(snapshot(), model=Model(), ledger=book)
    second_receipt = asyncio.run(execute_hunter_entry(second, ledger=book, rpc=Rpc(),
                          buyer=Buyer(), store=Store(), wallet=wallet,
                          current_snapshot=snapshot))
    assert second_receipt["spent_cents"] == 500
    keys = [row[0] for row in book.db.execute(
        "SELECT intent FROM orders WHERE mint=? AND side='BUY'", (MINT,)).fetchall()]
    assert len(keys) == 2 and len(set(keys)) == 2
    book.close()


def test_a_regrowth_entry_uses_its_own_eligibility_check_and_is_tagged_by_origin(tmp_path, monkeypatch):
    """A regrowth candidate has no live recommendation snapshot entry to
    verify against (see decide_regrowth_rebuy) - final_eligibility_check
    must be used instead of the default can_submit/current_snapshot path,
    and the confirmed position must carry origin='regrowth' so it's
    tracked against its own open-position cap, not hunter-v1's fresh one."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    wallet = "synthetic-owner"

    class Rpc:
        async def token_balance(self, owner, mint):
            return SimpleNamespace(raw_amount=20_000_000 if mint == USDC_MINT else 0)
        async def mint_decimals(self, mint):
            return 6
        async def get_transaction(self, signature):
            def row(index, mint, amount):
                return {"accountIndex": index, "mint": mint, "owner": wallet,
                        "uiTokenAmount": {"amount": str(amount)}}
            return {"meta": {"err": None,
                    "preTokenBalances": [row(0, USDC_MINT, 20_000_000)],
                    "postTokenBalances": [row(0, USDC_MINT, 15_000_000), row(1, MINT, 100_000_000)]}}

    class Client:
        async def order(self, **kwargs):
            return {"inputMint": MINT, "outputMint": USDC_MINT,
                    "inAmount": str(kwargs["amount_raw"]), "outAmount": "4100000",
                    "otherAmountThreshold": "4000000", "priceImpact": "-1",
                    "slippageBps": 250}

    class Buyer:
        client = Client()
        max_price_impact_pct = 3
        max_slippage_bps = 300
        async def preflight(self, intent, rpc):
            return SimpleNamespace(prepared=SimpleNamespace(input_amount_raw=intent.amount_usdc_raw,
                minimum_output_raw=100_000_000, expected_output_raw=100_000_000))
        async def execute(self, prepared):
            return SimpleNamespace(signature="public-signature", input_amount_raw=5_000_000,
                                   output_amount_raw=100_000_000)

    class Store:
        connection = _no_legacy_claim()
        def begin_auto_buy_execution(self, **kwargs):
            return True
        def complete_auto_buy_execution(self, **kwargs):
            pass
        def save_owned_holding(self, holding):
            pass
        def arm_auto_sell(self, mint, **kwargs):
            pass

    decision = decide_hunter_entry(snapshot(), model=Model(), ledger=book)
    eligibility_calls = []

    async def _eligible() -> bool:
        eligibility_calls.append(True)
        return True

    def _unused_snapshot():
        pytest.fail("a regrowth entry must not fall back to the recommendation snapshot")

    receipt = asyncio.run(execute_hunter_entry(
        decision, ledger=book, rpc=Rpc(), buyer=Buyer(), store=Store(), wallet=wallet,
        current_snapshot=_unused_snapshot, final_eligibility_check=_eligible, origin="regrowth",
    ))
    assert receipt["spent_cents"] == 500
    assert len(eligibility_calls) == 2  # pre-reservation and pre-broadcast
    assert book.positions("hunter-v1")[0]["origin"] == "regrowth"
    book.close()


def test_a_regrowth_entry_is_blocked_routinely_when_its_eligibility_check_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    wallet = "synthetic-owner"

    class Rpc:
        async def token_balance(self, owner, mint):
            return SimpleNamespace(raw_amount=20_000_000 if mint == USDC_MINT else 0)
        async def mint_decimals(self, mint):
            return 6

    class Client:
        async def order(self, **kwargs):
            return {"inputMint": MINT, "outputMint": USDC_MINT,
                    "inAmount": str(kwargs["amount_raw"]), "outAmount": "4100000",
                    "otherAmountThreshold": "4000000", "priceImpact": "-1",
                    "slippageBps": 250}

    class Buyer:
        client = Client()
        max_price_impact_pct = 3
        max_slippage_bps = 300
        async def preflight(self, intent, rpc):
            return SimpleNamespace(prepared=SimpleNamespace(input_amount_raw=intent.amount_usdc_raw,
                minimum_output_raw=100_000_000, expected_output_raw=100_000_000))
        async def execute(self, prepared):
            pytest.fail("a failed eligibility re-check must not broadcast")

    decision = decide_hunter_entry(snapshot(), model=Model(), ledger=book)

    async def _never_eligible() -> bool:
        return False

    with pytest.raises(TrialHalted, match="buy signal expired"):
        asyncio.run(execute_hunter_entry(
            decision, ledger=book, rpc=Rpc(), buyer=Buyer(), store=None, wallet=wallet,
            current_snapshot=lambda: {}, final_eligibility_check=_never_eligible, origin="regrowth",
        ))
    assert not book.unresolved()
    book.close()


def test_execution_reserves_before_broadcast_and_requires_chain_deltas(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    decision = decide_hunter_entry(snapshot(), model=Model(), ledger=book)
    sequence = []
    wallet = "synthetic-owner"

    class Rpc:
        async def token_balance(self, owner, mint):
            return SimpleNamespace(raw_amount=20_000_000 if mint == USDC_MINT else 0)
        async def mint_decimals(self, mint):
            return 6
        async def get_transaction(self, signature):
            def row(index, mint, amount):
                return {"accountIndex": index, "mint": mint, "owner": wallet,
                        "uiTokenAmount": {"amount": str(amount)}}
            return {"meta": {"err": None,
                    "preTokenBalances": [row(0, USDC_MINT, 20_000_000)],
                    "postTokenBalances": [row(0, USDC_MINT, 15_000_000), row(1, MINT, 100_000_000)]}}

    class Client:
        async def order(self, **kwargs):
            return {"inputMint": MINT, "outputMint": USDC_MINT,
                    "inAmount": str(kwargs["amount_raw"]), "outAmount": "4100000",
                    "otherAmountThreshold": "4000000", "priceImpact": "-1",
                    "slippageBps": 250}

    class Buyer:
        client = Client()
        max_price_impact_pct = 3
        max_slippage_bps = 300
        async def preflight(self, intent, rpc):
            return SimpleNamespace(prepared=SimpleNamespace(input_amount_raw=intent.amount_usdc_raw,
                minimum_output_raw=100_000_000, expected_output_raw=100_000_000))
        async def execute(self, prepared):
            sequence.append("execute")
            assert book.unresolved()[0]["state"] == "RESERVED"
            assert sequence[0] == "claim"
            return SimpleNamespace(signature="public-signature", input_amount_raw=5_000_000,
                                   output_amount_raw=100_000_000)

    class Store:
        connection = _no_legacy_claim()
        def begin_auto_buy_execution(self, **kwargs):
            sequence.append("claim")
            return True
        def complete_auto_buy_execution(self, **kwargs):
            sequence.append("complete")
        def save_owned_holding(self, holding):
            assert holding.token_address == MINT
        def arm_auto_sell(self, mint, **kwargs):
            assert mint == MINT

    receipt = asyncio.run(execute_hunter_entry(decision, ledger=book, rpc=Rpc(),
                          buyer=Buyer(), store=Store(), wallet=wallet,
                          current_snapshot=snapshot))
    assert receipt["spent_cents"] == 500
    assert book.status()["agents"]["hunter-v1"]["open_positions"] == 1
    assert not book.unresolved()
    book.close()


def test_momentum_buy_entry_allows_a_wider_guard_ceiling(tmp_path, monkeypatch):
    """A MOMENTUM BUY-sourced entry already cleared a stricter confirmation
    bar than a calmer pullback-zone entry, so a wider (but still bounded)
    price-impact/slippage ceiling is allowed through - observed live:
    BETBOLT's approved buy was rejected at 2001 bps against the normal
    1700 bps limit despite a clean confirmed momentum signal."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    decision = LiveEntryDecision(
        agent="hunter-v1", mint=MINT, requested_cents=500, approved_cents=500,
        reason="test", candidate={"decision": "MOMENTUM BUY", "symbol": "TEST",
                                  "liquidity_usd": 60_000},
    )
    wallet = "synthetic-owner"

    class Rpc:
        async def token_balance(self, owner, mint):
            return SimpleNamespace(raw_amount=20_000_000 if mint == USDC_MINT else 0)
        async def mint_decimals(self, mint):
            return 6
        async def get_transaction(self, signature):
            def row(index, mint, amount):
                return {"accountIndex": index, "mint": mint, "owner": wallet,
                        "uiTokenAmount": {"amount": str(amount)}}
            return {"meta": {"err": None,
                    "preTokenBalances": [row(0, USDC_MINT, 20_000_000)],
                    "postTokenBalances": [row(0, USDC_MINT, 15_000_000), row(1, MINT, 100_000_000)]}}

    class Client:
        async def order(self, **kwargs):
            return {"inputMint": MINT, "outputMint": USDC_MINT,
                    "inAmount": str(kwargs["amount_raw"]), "outAmount": "4100000",
                    "otherAmountThreshold": "4000000", "priceImpact": "-1",
                    "slippageBps": 2500}

    class Buyer:
        client = Client()
        max_price_impact_pct = 20
        max_slippage_bps = 3000
        async def preflight(self, intent, rpc):
            return SimpleNamespace(prepared=SimpleNamespace(input_amount_raw=intent.amount_usdc_raw,
                minimum_output_raw=100_000_000, expected_output_raw=100_000_000))
        async def execute(self, prepared):
            return SimpleNamespace(signature="public-signature", input_amount_raw=5_000_000,
                                   output_amount_raw=100_000_000)

    class Store:
        connection = _no_legacy_claim()
        def begin_auto_buy_execution(self, **kwargs):
            return True
        def complete_auto_buy_execution(self, **kwargs):
            pass
        def save_owned_holding(self, holding):
            pass
        def arm_auto_sell(self, mint, **kwargs):
            pass

    receipt = asyncio.run(execute_hunter_entry(decision, ledger=book, rpc=Rpc(),
                          buyer=Buyer(), store=Store(), wallet=wallet,
                          current_snapshot=snapshot))
    assert receipt["spent_cents"] == 500
    book.close()


def test_non_momentum_buy_entry_rejects_a_wide_guard_ceiling(tmp_path, monkeypatch):
    """The wider ceiling is earned by the MOMENTUM BUY label specifically -
    a buyer configured that wide for any other entry source must still
    halt, same as before this feature existed."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    decision = LiveEntryDecision(
        agent="hunter-v1", mint=MINT, requested_cents=500, approved_cents=500,
        reason="test", candidate={"decision": "BUY NOW", "symbol": "TEST"},
    )
    wide_buyer = SimpleNamespace(max_price_impact_pct=20, max_slippage_bps=3000)
    with pytest.raises(TrialHalted, match="guarded price impact or slippage"):
        asyncio.run(execute_hunter_entry(decision, ledger=book, rpc=None,
                          buyer=wide_buyer, store=None, wallet="synthetic-owner",
                          current_snapshot=snapshot))
    book.close()


async def _reverse_quote(**kwargs):
    return {"inputMint": MINT, "outputMint": USDC_MINT,
            "inAmount": "100000000", "outAmount": "4100000",
            "otherAmountThreshold": "4000000", "priceImpact": "-1",
            "slippageBps": 250}


class _PreflightBuyer:
    """A buyer that always reaches the post-reservation wallet/signal recheck."""
    client = SimpleNamespace(order=_reverse_quote)
    max_price_impact_pct = 3
    max_slippage_bps = 300

    async def preflight(self, intent, rpc):
        return SimpleNamespace(prepared=SimpleNamespace(
            input_amount_raw=intent.amount_usdc_raw,
            minimum_output_raw=100_000_000, expected_output_raw=100_000_000))


def _store_stub():
    return SimpleNamespace(
        connection=_no_legacy_claim(),
        begin_auto_buy_execution=lambda **kwargs: True,
        complete_auto_buy_execution=lambda **kwargs: None,
    )


def test_stale_signal_after_reservation_releases_the_order_instead_of_halting(tmp_path, monkeypatch):
    """The candidate falling out of BUY_READY in the ~1s between reservation
    and broadcast is routine market movement for these fast tokens, not a
    wallet-integrity problem - it must release the reservation and let the
    trial keep running, not halt the whole session."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    decision = decide_hunter_entry(snapshot(), model=Model(), ledger=book)
    wallet = "synthetic-owner"

    class Rpc:
        calls = 0
        async def token_balance(self, owner, mint):
            Rpc.calls += 1
            return SimpleNamespace(raw_amount=20_000_000 if mint == USDC_MINT else 0)
        async def mint_decimals(self, mint):
            return 6

    snapshot_calls = []
    def flaky_snapshot():
        # First can_submit check (just before reserving) still sees the
        # candidate as BUY_READY; the second (just before broadcast, ~1s
        # later in production) sees it's moved on - the same real-world gap
        # observed tonight.
        snapshot_calls.append(1)
        return snapshot() if len(snapshot_calls) == 1 else snapshot(confirmations=0)

    with pytest.raises(ValueError, match="signal expired"):
        asyncio.run(execute_hunter_entry(
            decision, ledger=book, rpc=Rpc(), buyer=_PreflightBuyer(), store=_store_stub(),
            wallet=wallet, current_snapshot=flaky_snapshot,
        ))
    assert book.status()["status"] == "ACTIVE"
    assert not book.unresolved()
    book.close()


def test_already_held_after_reservation_releases_the_order_instead_of_halting(tmp_path, monkeypatch):
    """A concurrent/manual buy landing on the same mint between reservation
    and broadcast (the same CASHTAG-style scenario the pre-reservation check
    already treats as routine) must not halt the whole trial either."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    decision = decide_hunter_entry(snapshot(), model=Model(), ledger=book)
    wallet = "synthetic-owner"

    class Rpc:
        calls = 0
        async def token_balance(self, owner, mint):
            Rpc.calls += 1
            if mint == USDC_MINT:
                return SimpleNamespace(raw_amount=20_000_000)
            # Pre-reservation check (call 2) sees nothing held yet; the
            # post-reservation recheck (call 4) sees a balance appear.
            return SimpleNamespace(raw_amount=0 if Rpc.calls <= 2 else 100_000_000)
        async def mint_decimals(self, mint):
            return 6

    with pytest.raises(ValueError, match="already held"):
        asyncio.run(execute_hunter_entry(
            decision, ledger=book, rpc=Rpc(), buyer=_PreflightBuyer(), store=_store_stub(),
            wallet=wallet, current_snapshot=snapshot,
        ))
    assert book.status()["status"] == "ACTIVE"
    assert not book.unresolved()
    book.close()


def test_insufficient_usdc_after_reservation_still_halts_the_trial(tmp_path, monkeypatch):
    """Real wallet USDC coming up short against a reservation the ledger
    already believes is good is a genuine accounting mismatch, not routine
    market movement - this must keep halting the whole trial."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    decision = decide_hunter_entry(snapshot(), model=Model(), ledger=book)
    wallet = "synthetic-owner"

    class Rpc:
        calls = 0
        async def token_balance(self, owner, mint):
            Rpc.calls += 1
            if mint != USDC_MINT:
                return SimpleNamespace(raw_amount=0)
            # Pre-reservation check (call 1) sees enough; the post-reservation
            # recheck (call 3) sees the balance has dropped below the reserve.
            return SimpleNamespace(raw_amount=20_000_000 if Rpc.calls <= 2 else 1_000_000)
        async def mint_decimals(self, mint):
            return 6

    with pytest.raises(TrialHalted, match="USDC balance dropped"):
        asyncio.run(execute_hunter_entry(
            decision, ledger=book, rpc=Rpc(), buyer=_PreflightBuyer(), store=_store_stub(),
            wallet=wallet, current_snapshot=snapshot,
        ))
    assert book.unresolved()
    assert book.unresolved()[0]["state"] == "RESERVED"
    book.close()


def test_a_restart_at_the_same_ledger_path_can_still_buy_a_mint_the_last_session_aborted(tmp_path, monkeypatch):
    """The active ledger is always recreated at the same filename once the
    old one is archived away (that's the real restart procedure) - a mint
    whose buy attempt was reserved and claimed in a past session, then
    released without broadcasting, must still be buyable in a new one. This
    is exactly what happened live: CATEWALK's signal expired post-reservation
    in one session, and the next session's restart couldn't retry it because
    the claim key didn't actually vary between sessions."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    path = tmp_path / "launch_guard_live_trial.sqlite"
    wallet = "synthetic-owner"

    class SharedMainStore:
        """Mimics the real SQLiteStore's one-shot claim, shared across
        sessions like the real main database actually is."""
        connection = _no_legacy_claim()
        def __init__(self):
            self.claimed: set[str] = set()
        def begin_auto_buy_execution(self, *, event_key, **kwargs):
            if event_key in self.claimed:
                return False
            self.claimed.add(event_key)
            return True
        def complete_auto_buy_execution(self, **kwargs):
            pass
        def save_owned_holding(self, holding):
            pass
        def arm_auto_sell(self, mint, **kwargs):
            pass

    shared_store = SharedMainStore()

    class Rpc:
        async def token_balance(self, owner, mint):
            return SimpleNamespace(raw_amount=20_000_000 if mint == USDC_MINT else 0)
        async def mint_decimals(self, mint):
            return 6

    first = LiveTrialLedger(path)
    first.start()
    decision = decide_hunter_entry(snapshot(), model=Model(), ledger=first)
    snapshot_calls = []
    def flaky_snapshot():
        snapshot_calls.append(1)
        return snapshot() if len(snapshot_calls) == 1 else snapshot(confirmations=0)
    with pytest.raises(ValueError, match="signal expired"):
        asyncio.run(execute_hunter_entry(
            decision, ledger=first, rpc=Rpc(), buyer=_PreflightBuyer(), store=shared_store,
            wallet=wallet, current_snapshot=flaky_snapshot,
        ))
    first.close()
    path.unlink()  # the real restart procedure archives (moves) the old file away

    second = LiveTrialLedger(path)
    second.start()
    decision2 = decide_hunter_entry(snapshot(), model=Model(), ledger=second)

    class BroadcastRpc(Rpc):
        async def get_transaction(self, signature):
            def row(index, mint, amount):
                return {"accountIndex": index, "mint": mint, "owner": wallet,
                        "uiTokenAmount": {"amount": str(amount)}}
            return {"meta": {"err": None,
                    "preTokenBalances": [row(0, USDC_MINT, 20_000_000)],
                    "postTokenBalances": [row(0, USDC_MINT, 15_000_000), row(1, MINT, 100_000_000)]}}

    class Buyer(_PreflightBuyer):
        async def execute(self, prepared):
            return SimpleNamespace(signature="public-signature", input_amount_raw=5_000_000,
                                   output_amount_raw=100_000_000)

    receipt = asyncio.run(execute_hunter_entry(
        decision2, ledger=second, rpc=BroadcastRpc(), buyer=Buyer(), store=shared_store,
        wallet=wallet, current_snapshot=snapshot,
    ))
    assert receipt["spent_cents"] == 500
    second.close()


def _confirmed_position(book, *, agent="hunter-v1", mint=MINT, quantity_raw=12_290_598_152):
    book.reserve_buy(intent=f"{agent}:buy", agent=agent, mint=mint,
                     requested_cents=500, approved_cents=500)
    book.transition(f"{agent}:buy", "SUBMITTED", signature="buy-sig")
    book.confirm_buy(intent=f"{agent}:buy", signature="buy-sig", executed_cents=500,
                     quantity_raw=quantity_raw, decimals=6, entry_price=0.0004068,
                     entry_liquidity_usd=56528.68, verified_on_chain=True)


def test_a_second_sell_of_the_same_mint_after_a_full_round_trip_does_not_collide(tmp_path, monkeypatch):
    """A mint can legitimately be bought, sold, rebought, and sold again in
    the same session - the second sell's intent key must not collide with
    the first, already-CONFIRMED sell's primary key (observed live: a
    position round-tripped and its second sell was permanently blocked
    with "this exit stage was already confirmed", leaving it stuck with
    no way to ever exit for the rest of the session)."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    # A stale prior sell claim from an earlier, already-completed round
    # trip on this same mint, using the same base key shape a second
    # full-position SELL would otherwise reuse.
    stale_key = f"live-trial:{book.path.stem}:{book.started_at()}:hunter-v1:sell:{MINT}:SELL"
    book.db.execute(
        "INSERT INTO orders(intent,agent,side,mint,state,created_at) VALUES(?,?,'SELL',?,?,?)",
        (stale_key, "hunter-v1", MINT, "CONFIRMED", time.time()),
    )
    book.db.commit()

    _confirmed_position(book)
    wallet = "synthetic-owner"

    class Rpc:
        async def token_balance(self, owner, mint):
            return SimpleNamespace(raw_amount=12_290_598_152, decimals=6)
        async def get_transaction(self, signature):
            def row(index, mint, amount):
                return {"accountIndex": index, "mint": mint, "owner": wallet,
                        "uiTokenAmount": {"amount": str(amount)}}
            return {"meta": {"err": None,
                    "preTokenBalances": [row(0, MINT, 12_290_598_152)],
                    "postTokenBalances": [row(1, USDC_MINT, 10_000_000)]}}

    class Seller:
        max_price_impact_pct = 3
        max_slippage_bps = 300
        async def preflight(self, plan, rpc):
            return SimpleNamespace(prepared=SimpleNamespace(input_amount_raw=plan.amount_raw,
                minimum_output_raw=9_000_000, expected_output_raw=10_000_000,
                quoted_price_impact_pct=-1.0, quoted_slippage_bps=100))
        async def execute(self, prepared):
            return SimpleNamespace(signature="sell-signature", input_amount_raw=12_290_598_152,
                                   output_amount_raw=10_000_000)

    result = asyncio.run(execute_live_exit(
        ledger=book, agent="hunter-v1", mint=MINT, symbol="TEST", decision="SELL",
        position_value_usd=12, quote_age_seconds=2, liquidity_usd=60_000, fraction=1,
        rpc=Rpc(), seller=Seller(), store=_sell_store_stub(), wallet=wallet,
        current_exit_allowed=lambda: True,
    ))
    assert result["proceeds_usdc_raw"] == 10_000_000
    book.close()


def test_wallet_balance_dropping_to_zero_reconciles_instead_of_halting(tmp_path, monkeypatch):
    """The operator selling a position manually - exactly what happened live
    with CATEWALK - must not halt the whole trial; the ledger should just
    recognize nothing is left to exit and move on."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    _confirmed_position(book)

    class Rpc:
        async def token_balance(self, owner, mint):
            return SimpleNamespace(raw_amount=0, decimals=6)

    with pytest.raises(ValueError, match="outside the trial"):
        asyncio.run(execute_live_exit(
            ledger=book, agent="hunter-v1", mint=MINT, symbol="TEST", decision="SELL",
            position_value_usd=12, quote_age_seconds=2, liquidity_usd=60_000, fraction=1,
            rpc=Rpc(), seller=SimpleNamespace(max_price_impact_pct=3, max_slippage_bps=300),
            store=None, wallet="synthetic-owner",
            current_exit_allowed=lambda: True,
        ))
    assert not any(p["mint"] == MINT for p in book.positions("hunter-v1"))
    book.close()


def test_wallet_balance_partially_reduced_reconciles_the_remainder_instead_of_halting(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    _confirmed_position(book, quantity_raw=10_000_000)

    class Rpc:
        async def token_balance(self, owner, mint):
            return SimpleNamespace(raw_amount=4_000_000, decimals=6)

    with pytest.raises(ValueError, match="outside the trial"):
        asyncio.run(execute_live_exit(
            ledger=book, agent="hunter-v1", mint=MINT, symbol="TEST", decision="SELL",
            position_value_usd=12, quote_age_seconds=2, liquidity_usd=60_000, fraction=1,
            rpc=Rpc(), seller=SimpleNamespace(max_price_impact_pct=3, max_slippage_bps=300),
            store=None, wallet="synthetic-owner",
            current_exit_allowed=lambda: True,
        ))
    remaining = next(p for p in book.positions("hunter-v1") if p["mint"] == MINT)
    assert remaining["quantity_raw"] == 4_000_000
    book.close()


def test_wallet_balance_higher_than_tracked_still_halts(tmp_path, monkeypatch):
    """An *increase* over what the ledger tracks has no manual-sell
    explanation and stays a genuine halt for manual reconciliation."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    _confirmed_position(book, quantity_raw=10_000_000)

    class Rpc:
        async def token_balance(self, owner, mint):
            return SimpleNamespace(raw_amount=20_000_000, decimals=6)

    with pytest.raises(TrialHalted, match="balance differ"):
        asyncio.run(execute_live_exit(
            ledger=book, agent="hunter-v1", mint=MINT, symbol="TEST", decision="SELL",
            position_value_usd=12, quote_age_seconds=2, liquidity_usd=60_000, fraction=1,
            rpc=Rpc(), seller=SimpleNamespace(max_price_impact_pct=3, max_slippage_bps=300),
            store=None, wallet="synthetic-owner",
            current_exit_allowed=lambda: True,
        ))
    remaining = next(p for p in book.positions("hunter-v1") if p["mint"] == MINT)
    assert remaining["quantity_raw"] == 10_000_000
    book.close()


def test_portfolio_owned_exit_can_exceed_five_dollars_without_bypassing_guards(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    wallet = "synthetic-owner"

    class Rpc:
        async def token_balance(self, owner, mint):
            return SimpleNamespace(raw_amount=100_000_000, decimals=6)
        async def get_transaction(self, signature):
            def row(index, mint, amount):
                return {"accountIndex": index, "mint": mint, "owner": wallet,
                        "uiTokenAmount": {"amount": str(amount)}}
            return {"meta": {"err": None,
                    "preTokenBalances": [row(0, MINT, 100_000_000)],
                    "postTokenBalances": [row(1, USDC_MINT, 10_000_000)]}}

    class Seller:
        max_price_impact_pct = 3
        max_slippage_bps = 300
        async def preflight(self, plan, rpc):
            return SimpleNamespace(prepared=SimpleNamespace(input_amount_raw=plan.amount_raw,
                minimum_output_raw=9_000_000, expected_output_raw=10_000_000,
                quoted_price_impact_pct=-1.0, quoted_slippage_bps=100))
        async def execute(self, prepared):
            assert len(book.unresolved()) == 1
            return SimpleNamespace(signature="sell-signature", input_amount_raw=100_000_000,
                                   output_amount_raw=10_000_000)

    class Store:
        connection = _no_legacy_claim()
        def begin_auto_sell_execution(self, **kwargs):
            return True
        def complete_auto_sell_execution(self, **kwargs):
            pass

    result = asyncio.run(execute_live_exit(
        ledger=book, agent="portfolio-v1", mint=MINT, symbol="TEST",
        decision="SELL", position_value_usd=12, quote_age_seconds=2,
        liquidity_usd=60_000, fraction=1, rpc=Rpc(), seller=Seller(),
        store=Store(), wallet=wallet, current_exit_allowed=lambda: True,
    ))
    assert result["proceeds_usdc_raw"] == 10_000_000
    assert not book.unresolved()
    assert book.status()["total_remaining_buy_cap_cents"] == 6000
    book.close()


def test_full_liquidation_of_a_crashed_position_clears_the_two_dollar_floor(tmp_path, monkeypatch):
    """A position that crashed to a fraction of a dollar (observed live:
    BONEPHIL fell to $0.115) must still be fully sellable end to end - the
    deterministic arbiter check and the quoted-proceeds floor both have to
    drop below $2 together for a full SELL, not just one of them."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    wallet = "synthetic-owner"

    class Rpc:
        async def token_balance(self, owner, mint):
            return SimpleNamespace(raw_amount=100_000_000, decimals=6)
        async def get_transaction(self, signature):
            def row(index, mint, amount):
                return {"accountIndex": index, "mint": mint, "owner": wallet,
                        "uiTokenAmount": {"amount": str(amount)}}
            return {"meta": {"err": None,
                    "preTokenBalances": [row(0, MINT, 100_000_000)],
                    "postTokenBalances": [row(1, USDC_MINT, 150_000)]}}

    class Seller:
        max_price_impact_pct = 3
        max_slippage_bps = 300
        async def preflight(self, plan, rpc):
            return SimpleNamespace(prepared=SimpleNamespace(input_amount_raw=plan.amount_raw,
                minimum_output_raw=150_000, expected_output_raw=150_000,
                quoted_price_impact_pct=-1.0, quoted_slippage_bps=100))
        async def execute(self, prepared):
            return SimpleNamespace(signature="sell-signature", input_amount_raw=100_000_000,
                                   output_amount_raw=150_000)

    class Store:
        connection = _no_legacy_claim()
        def begin_auto_sell_execution(self, **kwargs):
            return True
        def complete_auto_sell_execution(self, **kwargs):
            pass

    result = asyncio.run(execute_live_exit(
        ledger=book, agent="portfolio-v1", mint=MINT, symbol="TEST",
        decision="SELL", position_value_usd=0.15, quote_age_seconds=2,
        liquidity_usd=60_000, fraction=1, rpc=Rpc(), seller=Seller(),
        store=Store(), wallet=wallet, current_exit_allowed=lambda: True,
    ))
    assert result["proceeds_usdc_raw"] == 150_000
    assert not book.unresolved()
    book.close()


def test_catastrophic_floor_rejects_an_implausible_quote_even_in_emergency_mode(tmp_path, monkeypatch):
    """A quote implying an 80%+ haircut versus the position's own marked
    value is the signature of a broken quote or a stale oracle price, not
    a real market - the wide emergency slippage/impact ceiling must not
    let it through. Position marked at $10; quote offers only $1 (10% of
    value, below the 25% catastrophic floor)."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    wallet = "synthetic-owner"

    class Rpc:
        async def token_balance(self, owner, mint):
            return SimpleNamespace(raw_amount=100_000_000, decimals=6)

    class Seller:
        max_price_impact_pct = 35
        max_slippage_bps = 4000
        async def preflight(self, plan, rpc):
            return SimpleNamespace(prepared=SimpleNamespace(input_amount_raw=plan.amount_raw,
                minimum_output_raw=1_000_000, expected_output_raw=1_000_000,
                quoted_price_impact_pct=-5.0, quoted_slippage_bps=200))

    with pytest.raises(TrialHalted, match="implausible loss"):
        asyncio.run(execute_live_exit(
            ledger=book, agent="portfolio-v1", mint=MINT, symbol="TEST",
            decision="SELL", position_value_usd=10, quote_age_seconds=2,
            liquidity_usd=60_000, fraction=1, rpc=Rpc(), seller=Seller(),
            store=None, wallet=wallet, current_exit_allowed=lambda: True,
            max_price_impact_pct=35, max_slippage_bps=4000,
        ))
    book.close()


def test_partial_sell_of_a_crashed_position_still_requires_the_two_dollar_floor(tmp_path, monkeypatch):
    """Only a full liquidation gets the lowered floor - a discretionary
    partial sell of a crashed position has no such justification and
    should keep failing the deterministic $2 check."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    wallet = "synthetic-owner"

    class Rpc:
        async def token_balance(self, owner, mint):
            return SimpleNamespace(raw_amount=100_000_000, decimals=6)

    seller = SimpleNamespace(max_price_impact_pct=3, max_slippage_bps=300)

    with pytest.raises(TrialHalted, match="EXIT BLOCKED"):
        asyncio.run(execute_live_exit(
            ledger=book, agent="portfolio-v1", mint=MINT, symbol="TEST",
            decision="TAKE_PARTIAL", position_value_usd=1.0, quote_age_seconds=2,
            liquidity_usd=60_000, fraction=0.5, rpc=Rpc(), seller=seller,
            store=None, wallet=wallet, current_exit_allowed=lambda: True,
        ))
    book.close()


def _sell_seller():
    return SimpleNamespace(
        max_price_impact_pct=3, max_slippage_bps=300,
        preflight=lambda plan, rpc: _sell_preflight(plan),
        execute=lambda prepared: _sell_execute(),
    )


async def _sell_preflight(plan):
    return SimpleNamespace(prepared=SimpleNamespace(
        input_amount_raw=plan.amount_raw, minimum_output_raw=9_000_000,
        expected_output_raw=10_000_000, quoted_price_impact_pct=-1.0, quoted_slippage_bps=100,
    ))


async def _sell_execute():
    return SimpleNamespace(signature="sell-signature", input_amount_raw=100_000_000,
                           output_amount_raw=10_000_000)


def _sell_store_stub():
    return SimpleNamespace(
        connection=_no_legacy_claim(),
        begin_auto_sell_execution=lambda **kwargs: True,
        complete_auto_sell_execution=lambda **kwargs: None,
    )


def test_sell_balance_drop_after_reservation_reconciles_instead_of_halting(tmp_path, monkeypatch):
    """The operator selling manually in the gap between reservation and
    broadcast - exactly what happened live with BlueRio - must not halt
    the whole trial; the ledger should recognize less is left to sell and
    move on, mirroring the buy-side fix for the same failure shape."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    _confirmed_position(book, quantity_raw=100_000_000)
    wallet = "synthetic-owner"

    class Rpc:
        calls = 0
        async def token_balance(self, owner, mint):
            Rpc.calls += 1
            return SimpleNamespace(raw_amount=100_000_000 if Rpc.calls <= 1 else 40_000_000, decimals=6)

    with pytest.raises(ValueError, match="likely sold outside the trial"):
        asyncio.run(execute_live_exit(
            ledger=book, agent="hunter-v1", mint=MINT, symbol="TEST", decision="SELL",
            position_value_usd=12, quote_age_seconds=2, liquidity_usd=60_000, fraction=1,
            rpc=Rpc(), seller=_sell_seller(), store=_sell_store_stub(), wallet=wallet,
            current_exit_allowed=lambda: True,
        ))
    assert not book.unresolved()
    remaining = next(p for p in book.positions("hunter-v1") if p["mint"] == MINT)
    assert remaining["quantity_raw"] == 40_000_000
    book.close()


def test_sell_balance_increase_after_reservation_still_halts(tmp_path, monkeypatch):
    """An *increase* over the pre-reservation balance has no manual-sell
    explanation and stays a genuine halt for manual reconciliation."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    _confirmed_position(book, quantity_raw=100_000_000)
    wallet = "synthetic-owner"

    class Rpc:
        calls = 0
        async def token_balance(self, owner, mint):
            Rpc.calls += 1
            return SimpleNamespace(raw_amount=100_000_000 if Rpc.calls <= 1 else 150_000_000, decimals=6)

    with pytest.raises(TrialHalted, match="balance increased"):
        asyncio.run(execute_live_exit(
            ledger=book, agent="hunter-v1", mint=MINT, symbol="TEST", decision="SELL",
            position_value_usd=12, quote_age_seconds=2, liquidity_usd=60_000, fraction=1,
            rpc=Rpc(), seller=_sell_seller(), store=_sell_store_stub(), wallet=wallet,
            current_exit_allowed=lambda: True,
        ))
    assert book.unresolved()
    assert book.unresolved()[0]["state"] == "RESERVED"
    book.close()


def test_sell_signal_expiring_after_reservation_releases_instead_of_halting(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    _confirmed_position(book, quantity_raw=100_000_000)
    wallet = "synthetic-owner"

    class Rpc:
        async def token_balance(self, owner, mint):
            return SimpleNamespace(raw_amount=100_000_000, decimals=6)

    allow_calls = []
    def flaky_exit_allowed():
        allow_calls.append(1)
        # Allowed for the two pre-broadcast checks (before and right after
        # reservation); revoked only for the final post-reservation recheck
        # this test targets.
        return len(allow_calls) <= 2

    with pytest.raises(ValueError, match="exit signal expired or changed"):
        asyncio.run(execute_live_exit(
            ledger=book, agent="hunter-v1", mint=MINT, symbol="TEST", decision="SELL",
            position_value_usd=12, quote_age_seconds=2, liquidity_usd=60_000, fraction=1,
            rpc=Rpc(), seller=_sell_seller(), store=_sell_store_stub(), wallet=wallet,
            current_exit_allowed=flaky_exit_allowed,
        ))
    assert not book.unresolved()
    remaining = next(p for p in book.positions("hunter-v1") if p["mint"] == MINT)
    assert remaining["quantity_raw"] == 100_000_000
    book.close()


def test_a_different_trial_session_can_still_sell_a_mint_the_last_session_claimed(tmp_path, monkeypatch):
    """A stopped-and-restarted trial (a fresh ledger file) must not inherit
    a stale claim on the shared main execution database from a different
    trial session that happened to sell the same mint."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")

    class Rpc:
        async def token_balance(self, owner, mint):
            return SimpleNamespace(raw_amount=100_000_000, decimals=6)
        async def get_transaction(self, signature):
            def row(index, mint, amount):
                return {"accountIndex": index, "mint": mint, "owner": "synthetic-owner",
                        "uiTokenAmount": {"amount": str(amount)}}
            return {"meta": {"err": None,
                    "preTokenBalances": [row(0, MINT, 100_000_000)],
                    "postTokenBalances": [row(1, USDC_MINT, 10_000_000)]}}

    class Seller:
        max_price_impact_pct = 3
        max_slippage_bps = 300
        async def preflight(self, plan, rpc):
            return SimpleNamespace(prepared=SimpleNamespace(input_amount_raw=plan.amount_raw,
                minimum_output_raw=9_000_000, expected_output_raw=10_000_000,
                quoted_price_impact_pct=-1.0, quoted_slippage_bps=100))
        async def execute(self, prepared):
            return SimpleNamespace(signature="sell-signature", input_amount_raw=100_000_000,
                                   output_amount_raw=10_000_000)

    class SharedMainStore:
        """Mimics the real SQLiteStore's one-shot claim (INSERT OR IGNORE on
        event_key), shared across trial sessions like the real main
        database actually is - only one LiveTrialLedger per test in the
        other tests here, so nothing else exercises that sharing."""
        connection = _no_legacy_claim()

        def __init__(self):
            self.claimed: set[str] = set()

        def begin_auto_sell_execution(self, *, event_key, **kwargs):
            if event_key in self.claimed:
                return False
            self.claimed.add(event_key)
            return True

        def complete_auto_sell_execution(self, **kwargs):
            pass

    shared_store = SharedMainStore()
    kwargs = dict(
        agent="portfolio-v1", mint=MINT, symbol="TEST", decision="SELL",
        position_value_usd=12, quote_age_seconds=2, liquidity_usd=60_000,
        fraction=1, rpc=Rpc(), seller=Seller(), store=shared_store,
        wallet="synthetic-owner", current_exit_allowed=lambda: True,
    )

    first = LiveTrialLedger(tmp_path / "session_one.sqlite")
    first.start()
    first_result = asyncio.run(execute_live_exit(ledger=first, **kwargs))
    assert first_result["proceeds_usdc_raw"] == 10_000_000
    first.close()

    second = LiveTrialLedger(tmp_path / "session_two.sqlite")
    second.start()
    second_result = asyncio.run(execute_live_exit(ledger=second, **kwargs))
    assert second_result["proceeds_usdc_raw"] == 10_000_000
    second.close()


def test_signal_expiring_during_simulation_never_reserves_or_broadcasts(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    checks = iter((True, False))

    class Rpc:
        async def token_balance(self, owner, mint):
            return SimpleNamespace(raw_amount=100_000_000, decimals=6)

    class Seller:
        max_price_impact_pct = 3
        max_slippage_bps = 300

        async def preflight(self, plan, rpc):
            return SimpleNamespace(prepared=SimpleNamespace(
                input_amount_raw=plan.amount_raw, minimum_output_raw=9_000_000,
                quoted_price_impact_pct=-1, quoted_slippage_bps=100,
            ))

        async def execute(self, prepared):
            pytest.fail("a stale signal cannot broadcast")

    with pytest.raises(TrialHalted, match="EXIT BLOCKED: exit signal changed"):
        asyncio.run(execute_live_exit(
            ledger=book, agent="portfolio-v1", mint=MINT, symbol="TEST",
            decision="SELL", position_value_usd=12, quote_age_seconds=2,
            liquidity_usd=60_000, fraction=1, rpc=Rpc(), seller=Seller(),
            store=None, wallet="synthetic-owner",
            current_exit_allowed=lambda: next(checks),
        ))
    assert not book.unresolved()
    assert book.status()["status"] == "ACTIVE"
    book.close()


def _quote(price=1.0, **overrides):
    data = dict(mint=MINT, symbol="TEST", price_sol=price, price_usd=price,
               liquidity_usd=REGROWTH_MIN_LIQUIDITY_USD, market_cap_usd=100_000,
               pair_address="pair", pair_created_at_ms=1, buys_m5=20, sells_m5=5,
               volume_m5_usd=3_000, price_change_m5_pct=2)
    data.update(overrides)
    return MarketQuote(**data)


class FakeOracle:
    def __init__(self, quote):
        self.quote_returned = quote
        self.calls = 0

    async def quote(self, mint):
        self.calls += 1
        return self.quote_returned


def _seed_closed_position(book, *, mint, exit_price, agent="hunter-v1", now=None, label=None,
                          cost_cents=50):
    """A full buy+sell round trip through the public ledger API, producing
    a real closed_positions row the way a genuine round trip would.

    cost_cents defaults low relative to a typical exit_price around $1 (a
    proceeds_cents of ~100) so seeding a closed position for regrowth
    doesn't itself accidentally realize a loss and trip the (unrelated)
    daily-loss-limit or mint-loss-streak gates the tests aren't exercising -
    pass a higher cost_cents to deliberately seed a losing round trip.
    """
    at = time.time() if now is None else now
    tag = f"{mint}:{label if label is not None else at}"
    book.reserve_buy(intent=f"seed-buy:{tag}", agent=agent, mint=mint, requested_cents=500,
                     approved_cents=500, now=at)
    book.transition(f"seed-buy:{tag}", "SUBMITTED", signature=f"seed-buy-sig:{tag}")
    book.confirm_buy(intent=f"seed-buy:{tag}", signature=f"seed-buy-sig:{tag}", executed_cents=cost_cents,
                     quantity_raw=1, decimals=0, entry_price=1.0,
                     entry_liquidity_usd=60000, verified_on_chain=True)
    book.reserve_sell(intent=f"seed-sell:{tag}", agent=agent, mint=mint, now=at)
    book.transition(f"seed-sell:{tag}", "SUBMITTED", signature=f"seed-sell-sig:{tag}")
    book.confirm_sell(intent=f"seed-sell:{tag}", signature=f"seed-sell-sig:{tag}", quantity_raw=1,
                      proceeds_cents=round(exit_price * 100), verified_on_chain=True)


def test_regrowth_bar_clears_on_genuine_growth_and_rejects_a_weak_signal():
    assert _regrowth_bar_clears(_quote(price=1 + REGROWTH_MIN_GROWTH_PCT / 100 * 1.5), exit_price=1.0)
    # Below the growth threshold.
    assert not _regrowth_bar_clears(_quote(price=1.05), exit_price=1.0)
    # Growth is there, but momentum/liquidity/pressure are not.
    assert not _regrowth_bar_clears(_quote(price=2, liquidity_usd=1_000), exit_price=1.0)
    assert not _regrowth_bar_clears(_quote(price=2, price_change_m5_pct=-1), exit_price=1.0)
    assert not _regrowth_bar_clears(_quote(price=2, buys_m5=2, sells_m5=10), exit_price=1.0)
    assert not _regrowth_bar_clears(None, exit_price=1.0)


def test_decide_regrowth_rebuy_returns_none_with_no_closed_positions(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    model = Model()
    oracle = FakeOracle(_quote(price=2.0))
    assert asyncio.run(decide_regrowth_rebuy(model=model, ledger=book, oracle=oracle)) is None
    assert oracle.calls == 0
    assert model.calls == 0
    book.close()


def test_decide_regrowth_rebuy_skips_the_model_when_the_bar_is_not_cleared(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    _seed_closed_position(book, mint=MINT, exit_price=1.0)
    model = Model()
    oracle = FakeOracle(_quote(price=1.05))  # below REGROWTH_MIN_GROWTH_PCT
    assert asyncio.run(decide_regrowth_rebuy(model=model, ledger=book, oracle=oracle)) is None
    assert oracle.calls == 1
    assert model.calls == 0
    book.close()


def test_decide_regrowth_rebuy_requires_consecutive_confirmations_not_just_one(tmp_path, monkeypatch):
    """A single snapshot clearing the growth bar is much weaker evidence
    than every other buy path in this system requires - it must clear the
    bar REGROWTH_CONFIRMATION_POLLS cycles in a row before the model is
    even asked, not just once."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    _seed_closed_position(book, mint=MINT, exit_price=1.0)
    model = Model(requested=5)
    oracle = FakeOracle(_quote(price=1 + REGROWTH_MIN_GROWTH_PCT / 100 * 1.5))
    confirmations: dict[str, int] = {}
    for _ in range(REGROWTH_CONFIRMATION_POLLS - 1):
        assert asyncio.run(decide_regrowth_rebuy(
            model=model, ledger=book, oracle=oracle, regrowth_confirmation_counts=confirmations,
        )) is None
    assert model.calls == 0
    decision = asyncio.run(decide_regrowth_rebuy(
        model=model, ledger=book, oracle=oracle, regrowth_confirmation_counts=confirmations,
    ))
    assert decision is not None
    assert decision.mint == MINT
    assert decision.approved_cents == 500
    assert model.calls == 1
    book.close()


def test_decide_regrowth_rebuy_resets_the_streak_when_the_bar_stops_clearing(tmp_path, monkeypatch):
    """A mint that clears the bar, then drops back below it, must re-earn
    confirmation from scratch - a blip doesn't get to keep partial credit
    toward the next genuine run."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    _seed_closed_position(book, mint=MINT, exit_price=1.0)
    model = Model(requested=5)
    growing = FakeOracle(_quote(price=1 + REGROWTH_MIN_GROWTH_PCT / 100 * 1.5))
    flat = FakeOracle(_quote(price=1.01))  # below the growth bar
    confirmations: dict[str, int] = {}
    assert asyncio.run(decide_regrowth_rebuy(
        model=model, ledger=book, oracle=growing, regrowth_confirmation_counts=confirmations,
    )) is None
    assert confirmations[MINT] == 1
    assert asyncio.run(decide_regrowth_rebuy(
        model=model, ledger=book, oracle=flat, regrowth_confirmation_counts=confirmations,
    )) is None
    assert MINT not in confirmations
    assert model.calls == 0
    book.close()


def test_decide_regrowth_rebuy_has_its_own_open_position_cap_separate_from_fresh(tmp_path, monkeypatch):
    """A regrowth re-entry never competes with hunter-v1's own fresh-
    discovery slots (see decide_hunter_entry's fresh-only count) - it's
    blocked by its own, separate cap instead."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    other_mint = "B" * 44
    _seed_closed_position(book, mint=other_mint, exit_price=1.0)
    # A regrowth position already open occupies the one regrowth slot.
    book.reserve_buy(intent="already-open", agent="hunter-v1", mint="C" * 44,
                     requested_cents=500, approved_cents=500)
    book.transition("already-open", "SUBMITTED", signature="already-open-sig")
    book.confirm_buy(intent="already-open", signature="already-open-sig", executed_cents=500,
                     quantity_raw=1, decimals=0, entry_price=1.0,
                     entry_liquidity_usd=60000, verified_on_chain=True, origin="regrowth")
    model = Model()
    oracle = FakeOracle(_quote(price=1 + REGROWTH_MIN_GROWTH_PCT / 100 * 1.5))
    assert asyncio.run(decide_regrowth_rebuy(model=model, ledger=book, oracle=oracle)) is None
    assert model.calls == 0
    book.close()


def test_decide_regrowth_rebuy_blocks_a_mint_after_two_consecutive_losses(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    # cost_cents=200 against a $1 (100-cent) exit realizes a loss each time -
    # small enough (total -$2) to stay under the $3 daily-loss cap, so the
    # loss-streak block is what's actually being exercised here, not that.
    _seed_closed_position(book, mint=MINT, exit_price=1.0, label="first", cost_cents=200)
    _seed_closed_position(book, mint=MINT, exit_price=1.0, label="second", cost_cents=200)
    model = Model()
    oracle = FakeOracle(_quote(price=1 + REGROWTH_MIN_GROWTH_PCT / 100 * 1.5))
    assert asyncio.run(decide_regrowth_rebuy(model=model, ledger=book, oracle=oracle)) is None
    assert model.calls == 0
    book.close()


def test_decide_regrowth_rebuy_only_logs_a_loss_streak_block_once_per_reason(tmp_path, monkeypatch):
    """A mint stuck here clears the growth bar every cycle (it's still
    climbing) while permanently failing the loss-streak check - logging
    that every cycle would grow the ledger unbounded, same problem
    decide_hunter_entry's buy_zone_skip_reasons solves for the fresh
    path."""
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    _seed_closed_position(book, mint=MINT, exit_price=1.0, label="first", cost_cents=200)
    _seed_closed_position(book, mint=MINT, exit_price=1.0, label="second", cost_cents=200)
    model = Model()
    oracle = FakeOracle(_quote(price=1 + REGROWTH_MIN_GROWTH_PCT / 100 * 1.5))
    skip_reasons: dict[str, str] = {}
    confirmations: dict[str, int] = {}
    for _ in range(REGROWTH_CONFIRMATION_POLLS + 2):
        assert asyncio.run(decide_regrowth_rebuy(
            model=model, ledger=book, oracle=oracle, regrowth_skip_reasons=skip_reasons,
            regrowth_confirmation_counts=confirmations,
        )) is None
    blocked = book.db.execute(
        "SELECT COUNT(*) FROM decisions WHERE agent='hunter-v1' AND mint=? AND state='BLOCKED'", (MINT,),
    ).fetchone()[0]
    assert blocked == 1
    assert model.calls == 0
    book.close()


def test_decide_regrowth_rebuy_skips_the_model_once_the_daily_loss_limit_is_breached(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LIVE_KILL_SWITCH", "false")
    book = LiveTrialLedger(tmp_path / "trial.sqlite")
    book.start()
    _seed_closed_position(book, mint=MINT, exit_price=1.0)
    book.db.execute(
        "INSERT INTO orders(intent,agent,side,mint,state,realized_cents,created_at) "
        "VALUES(?,?,'SELL',?,'CONFIRMED',?,?)",
        ("daily-loss-seed", "hunter-v1", "B" * 44, -350, time.time()),
    )
    book.db.commit()
    model = Model()
    oracle = FakeOracle(_quote(price=1 + REGROWTH_MIN_GROWTH_PCT / 100 * 1.5))
    assert asyncio.run(decide_regrowth_rebuy(model=model, ledger=book, oracle=oracle)) is None
    assert model.calls == 0
    book.close()
