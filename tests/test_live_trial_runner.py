"""Only decision and submission guards; this module cannot broadcast."""
import asyncio
import time
from types import SimpleNamespace
import pytest

from solana_launch_guard.live_trial_ledger import LiveTrialLedger, TrialHalted
from solana_launch_guard.live_trial_runner import can_submit, decide_hunter_entry, execute_hunter_entry, execute_live_exit
from solana_launch_guard.execution import USDC_MINT


MINT = "A" * 44  # synthetic Base58-looking test mint


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
