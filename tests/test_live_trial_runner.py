"""Only decision and submission guards; this module cannot broadcast."""
import asyncio
import time
from types import SimpleNamespace
import pytest

from solana_launch_guard.live_trial_ledger import LiveTrialLedger, TrialHalted
from solana_launch_guard.live_trial_runner import can_submit, decide_hunter_entry, execute_hunter_entry, execute_live_exit
from solana_launch_guard.execution import USDC_MINT


MINT = "A" * 44  # synthetic Base58-looking test mint


def _no_legacy_claim() -> SimpleNamespace:
    """A store.connection stub reporting no pre-existing legacy-key claim."""
    return SimpleNamespace(execute=lambda *a, **k: SimpleNamespace(fetchone=lambda: None))


def snapshot(*, confirmations=3, momentum="RISING", liquidity=60000):
    now = time.time()
    return {"generated_at": now, "candidates": [{
        "chain": "solana", "mint": MINT, "symbol": "TEST",
        "quoted_at": now, "price": .005, "peak_price": .006,
        "pullback_from_peak_pct": 16, "liquidity_usd": liquidity,
        "initial_liquidity_usd": liquidity,
        "entry_confirmation_count": confirmations, "entry_confirmation_required": 3,
        "momentum_label": momentum, "price_change_m5_pct": 2,
        "volume_label": "RISING", "buys_m5": 20, "sells_m5": 10,
        "buy_sell_ratio": 2, "risk_label": "MEDIUM", "signal_score": 90,
        "decision": "BUY NOW",
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
