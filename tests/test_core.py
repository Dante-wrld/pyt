from __future__ import annotations

import asyncio
import io
import sqlite3
import time
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest
from solders.keypair import Keypair

from solana_launch_guard.app import (
    LaunchGuard,
    _is_stock_token_symbol,
    auto_buy_discovery_rejection,
    auto_rebuy_recovery_assessment,
    preflight_auto_buy,
    preflight_auto_rebuy,
    preflight_auto_sell,
    preflight_owned_auto_sell,
    reconcile_auto_sell_review,
)
from solana_launch_guard.config import Settings
from solana_launch_guard.core import (
    Launch,
    PaperBroker,
    RiskEngine,
    SQLiteStore,
    indicative_price,
)
from solana_launch_guard.execution import (
    USDC_MINT,
    BuyIntent,
    BuyPreflightReceipt,
    BuyReceipt,
    JupiterExecutionError,
    JupiterRequestError,
    JupiterSwapClient,
    PortfolioSignalExitPlanner,
    PreflightReceipt,
    PreparedBuy,
    PreparedSell,
    ProfitLadder,
    SellIntent,
    SellReceipt,
    SolanaAutoBuyer,
    SolanaAutoSeller,
    store_fomo_solana_key,
)
from solana_launch_guard.intelligence import CoinIntelligence
from solana_launch_guard.market import DexScreenerOracle, MarketQuote
from solana_launch_guard.multichain import EvmRpc, EvmTransfer, HyperCoreWatcher
from solana_launch_guard.notifications import (
    DecisionNotifier,
    PortfolioNotifier,
    format_candidate_notification,
    format_portfolio_notification,
)
from solana_launch_guard.portfolio import (
    OwnedHolding,
    PortfolioAdvisor,
    PortfolioSignal,
    build_portfolio_snapshot,
    format_portfolio_dashboard,
)
from solana_launch_guard.pullback_tracking import PullbackTracker
from solana_launch_guard.recommendations import (
    RecommendationBook,
    RecommendationCandidate,
    build_snapshot,
    format_dashboard,
    format_recommendations,
    read_snapshot,
    write_snapshot,
)
from solana_launch_guard.strategy import AdaptiveStrategy
from solana_launch_guard.wallet import (
    SolanaRpc,
    SolanaTokenHolding,
    parse_wallet_trades,
)


def test_profit_ladder_recovers_principal_then_sells_half() -> None:
    ladder = ProfitLadder()
    first = ladder.plan(
        mint="MintProfit111",
        symbol="WIN",
        stage=0,
        balance_raw=100_000_000,
        decimals=6,
        current_price_usd=2.10,
        entry_price_usd=1.0,
        original_cost_usd=100.0,
    )

    assert first is not None
    assert first.amount_raw == 47_619_048
    assert first.target_output_raw == 100_000_000
    assert first.trigger_multiple == 2.0

    second = ladder.plan(
        mint="MintProfit111",
        symbol="WIN",
        stage=1,
        balance_raw=52_380_952,
        decimals=6,
        current_price_usd=3.0,
        entry_price_usd=1.0,
        original_cost_usd=100.0,
    )

    assert second is not None
    assert second.amount_raw == 26_190_476
    assert second.target_output_raw == 78_571_428
    assert ladder.plan(
        mint="MintProfit111",
        symbol="WIN",
        stage=2,
        balance_raw=1,
        decimals=6,
        current_price_usd=10,
        entry_price_usd=1,
        original_cost_usd=100,
    ) is None


def test_profit_ladder_requires_trigger_and_cost_basis() -> None:
    ladder = ProfitLadder()
    values = {
        "mint": "MintProfit111",
        "symbol": "WIN",
        "stage": 0,
        "balance_raw": 100_000_000,
        "decimals": 6,
        "current_price_usd": 1.99,
        "entry_price_usd": 1.0,
        "original_cost_usd": 100.0,
    }
    assert ladder.plan(**values) is None
    values["current_price_usd"] = 2.1
    values["original_cost_usd"] = None
    assert ladder.plan(**values) is None


def test_portfolio_signal_exit_planner_uses_configured_fractions() -> None:
    planner = PortfolioSignalExitPlanner(
        take_partial_fraction=0.5,
        protect_profit_fraction=0.75,
        exit_warning_fraction=1.0,
    )

    partial = planner.plan(
        mint="MintOwned111",
        symbol="OWN",
        decision="TAKE PARTIAL",
        reason="profit target reached",
        balance_raw=101,
        decimals=6,
    )
    exit_all = planner.plan(
        mint="MintOwned111",
        symbol="OWN",
        decision="EXIT WARNING",
        reason="momentum reversal",
        balance_raw=101,
        decimals=6,
    )

    assert partial is not None
    assert partial.amount_raw == 50
    assert partial.target_output_raw is None
    assert partial.event_key.endswith("portfolio-signal:take-partial")
    assert exit_all is not None
    assert exit_all.amount_raw == 101
    assert planner.plan(
        mint="MintOwned111",
        symbol="OWN",
        decision="HOLD",
        reason="inside limits",
        balance_raw=101,
        decimals=6,
    ) is None


def test_profit_ladder_preflight_uses_next_stage_amount_without_target() -> None:
    intent = ProfitLadder().preflight_plan(
        mint="MintProfit111",
        symbol="WIN",
        stage=0,
        balance_raw=100_000_000,
        decimals=6,
        entry_price_usd=1.0,
        original_cost_usd=100.0,
    )

    assert intent.amount_raw == 50_000_000
    assert intent.target_output_raw is None
    assert intent.event_key == "solana:MintProfit111:preflight:0"
    assert "never broadcast" in intent.reason


def test_profit_ladder_preflight_refuses_completed_ladder() -> None:
    with pytest.raises(ValueError, match="already complete"):
        ProfitLadder().preflight_plan(
            mint="MintProfit111",
            symbol="WIN",
            stage=2,
            balance_raw=100_000_000,
            decimals=6,
            entry_price_usd=1.0,
            original_cost_usd=100.0,
        )


def test_auto_seller_prepares_and_executes_confirmed_order() -> None:
    class FakeSigner:
        public_key = "Wallet111"

        def sign(self, transaction_b64: str) -> str:
            assert transaction_b64 == "unsigned"
            return "signed"

    class FakeClient:
        async def order(self, **values: object) -> dict[str, object]:
            amount = int(values["amount_raw"])
            return {
                "inputMint": values["input_mint"],
                "outputMint": (
                    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
                ),
                "inAmount": str(amount),
                "outAmount": str(amount * 2),
                "otherAmountThreshold": str(amount * 2),
                "priceImpact": 0.1,
                "transaction": "unsigned",
                "requestId": "request-1",
                "lastValidBlockHeight": "123",
            }

        async def execute(self, **values: object) -> dict[str, object]:
            assert values["signed_transaction"] == "signed"
            assert values["last_valid_block_height"] == "123"
            return {
                "status": "Success",
                "code": 0,
                "signature": "signature-1",
                "totalInputAmount": "50000000",
                "totalOutputAmount": "100000000",
            }

    intent = SellIntent(
        mint="MintProfit111",
        symbol="WIN",
        stage=0,
        event_key="event-1",
        amount_raw=50_000_000,
        balance_raw=100_000_000,
        decimals=6,
        trigger_multiple=2,
        current_multiple=2.1,
        target_output_raw=100_000_000,
        reason="recover principal",
    )
    seller = SolanaAutoSeller(client=FakeClient(), signer=FakeSigner())
    prepared = asyncio.run(seller.prepare(intent))
    receipt = asyncio.run(seller.execute(prepared))

    assert isinstance(prepared, PreparedSell)
    assert prepared.minimum_output_raw == 100_000_000
    assert receipt.signature == "signature-1"
    assert receipt.output_amount_raw == 100_000_000


def test_auto_seller_preflight_signs_and_simulates_without_execution() -> None:
    executed = False

    class FakeSigner:
        public_key = "Wallet111"

        def sign(self, transaction_b64: str) -> str:
            assert transaction_b64 == "unsigned"
            return "signed"

    class FakeClient:
        async def order(self, **values: object) -> dict[str, object]:
            assert values["exclude_routers"] == ("jupiterz",)
            amount = int(values["amount_raw"])
            return {
                "inputMint": values["input_mint"],
                "outputMint": (
                    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
                ),
                "inAmount": str(amount),
                "outAmount": "10000000",
                "otherAmountThreshold": "9500000",
                "priceImpact": 0.2,
                "transaction": "unsigned",
                "requestId": "request-preflight",
            }

        async def execute(self, **_values: object) -> dict[str, object]:
            nonlocal executed
            executed = True
            raise AssertionError("preflight must never execute")

    class FakeSimulator:
        async def simulate_transaction(
            self, signed_transaction_b64: str
        ) -> dict[str, object]:
            assert signed_transaction_b64 == "signed"
            return {"err": None, "logs": ["one", "two"], "unitsConsumed": 42}

    intent = ProfitLadder().preflight_plan(
        mint="MintProfit111",
        symbol="WIN",
        stage=0,
        balance_raw=100_000_000,
        decimals=6,
        entry_price_usd=1,
        original_cost_usd=100,
    )
    seller = SolanaAutoSeller(client=FakeClient(), signer=FakeSigner())

    receipt = asyncio.run(seller.preflight(intent, FakeSimulator()))

    assert receipt.broadcast is False
    assert receipt.units_consumed == 42
    assert receipt.log_count == 2
    assert executed is False


def test_auto_seller_rejects_adverse_negative_price_impact() -> None:
    class FakeSigner:
        public_key = "Wallet111"

        def sign(self, transaction_b64: str) -> str:
            return transaction_b64

    class FakeClient:
        async def order(self, **values: object) -> dict[str, object]:
            return {
                "inputMint": values["input_mint"],
                "outputMint": (
                    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
                ),
                "inAmount": str(values["amount_raw"]),
                "outAmount": "10000000",
                "otherAmountThreshold": "9500000",
                "priceImpact": -5.1,
                "transaction": "unsigned",
                "requestId": "request-impact",
            }

        async def execute(self, **_values: object) -> dict[str, object]:
            raise AssertionError("unsafe price impact must never execute")

    intent = ProfitLadder().preflight_plan(
        mint="MintProfit111",
        symbol="WIN",
        stage=0,
        balance_raw=100_000_000,
        decimals=6,
        entry_price_usd=1,
        original_cost_usd=100,
    )
    seller = SolanaAutoSeller(
        client=FakeClient(), signer=FakeSigner(), max_price_impact_pct=5
    )

    with pytest.raises(ValueError, match="price impact"):
        asyncio.run(seller.prepare(intent))


def test_auto_seller_rejects_quote_above_slippage_limit() -> None:
    class FakeSigner:
        public_key = "Wallet111"

        def sign(self, transaction_b64: str) -> str:
            return transaction_b64

    class FakeClient:
        async def order(self, **values: object) -> dict[str, object]:
            return {
                "inputMint": values["input_mint"],
                "outputMint": USDC_MINT,
                "inAmount": str(values["amount_raw"]),
                "outAmount": "10000000",
                "otherAmountThreshold": "8000000",
                "priceImpact": 1.0,
                "slippageBps": 2000,
                "transaction": "unsigned",
                "requestId": "request-sell",
            }

        async def execute(self, **_values: object) -> dict[str, object]:
            raise AssertionError("unsafe sell must never execute")

    intent = SellIntent(
        mint="MintOwned111",
        symbol="OWN",
        stage=12,
        event_key="signal-exit",
        amount_raw=100_000_000,
        balance_raw=100_000_000,
        decimals=6,
        trigger_multiple=0,
        current_multiple=0,
        target_output_raw=None,
        reason="exit warning",
    )
    seller = SolanaAutoSeller(
        client=FakeClient(), signer=FakeSigner(), max_slippage_bps=500
    )

    with pytest.raises(ValueError, match="slippage 2000 bps"):
        asyncio.run(seller.prepare(intent))


def test_auto_seller_can_floor_guard_percentages_when_opted_in() -> None:
    class FakeSigner:
        public_key = "Wallet111"

        def sign(self, transaction_b64: str) -> str:
            return transaction_b64

    class FakeClient:
        async def order(self, **values: object) -> dict[str, object]:
            return {
                "inputMint": values["input_mint"],
                "outputMint": USDC_MINT,
                "inAmount": str(values["amount_raw"]),
                "outAmount": "10000000",
                "otherAmountThreshold": "9500000",
                "priceImpact": -5.99,
                "slippageBps": 599,
                "transaction": "unsigned",
                "requestId": "request-floored-sell",
            }

    intent = SellIntent(
        mint="MintOwned111",
        symbol="OWN",
        stage=10,
        event_key="signal-partial",
        amount_raw=50_000_000,
        balance_raw=100_000_000,
        decimals=6,
        trigger_multiple=0,
        current_multiple=0,
        target_output_raw=None,
        reason="take partial",
    )
    seller = SolanaAutoSeller(
        client=FakeClient(),
        signer=FakeSigner(),
        max_price_impact_pct=5,
        max_slippage_bps=500,
        floor_percentages=True,
    )

    prepared = asyncio.run(seller.prepare(intent))

    assert prepared.price_impact_pct == -5
    assert prepared.quoted_price_impact_pct == -5.99
    assert prepared.slippage_bps == 500
    assert prepared.quoted_slippage_bps == 599


def test_auto_seller_adaptive_preflight_halves_until_guard_passes() -> None:
    requested: list[int] = []

    class FakeSigner:
        public_key = "Wallet111"

        def sign(self, transaction_b64: str) -> str:
            return f"signed:{transaction_b64}"

    class FakeClient:
        async def order(self, **values: object) -> dict[str, object]:
            amount = int(values["amount_raw"])
            requested.append(amount)
            return {
                "inputMint": values["input_mint"],
                "outputMint": USDC_MINT,
                "inAmount": str(amount),
                "outAmount": "10000000",
                "otherAmountThreshold": "9500000",
                "priceImpact": 1.0,
                "slippageBps": 1000 if amount > 50 else 500,
                "transaction": f"unsigned-{amount}",
                "requestId": f"request-{amount}",
            }

    class FakeSimulator:
        async def simulate_transaction(
            self, signed_transaction_b64: str
        ) -> dict[str, object]:
            assert signed_transaction_b64 == "signed:unsigned-50"
            return {"err": None, "logs": ["ok"], "unitsConsumed": 77}

    intent = SellIntent(
        mint="MintOwned111",
        symbol="OWN",
        stage=12,
        event_key="signal-exit:chunk:0",
        amount_raw=100,
        balance_raw=100,
        decimals=0,
        trigger_multiple=0,
        current_multiple=0,
        target_output_raw=None,
        reason="exit warning",
    )
    seller = SolanaAutoSeller(
        client=FakeClient(), signer=FakeSigner(), max_slippage_bps=500
    )

    receipt = asyncio.run(
        seller.preflight_adaptive(
            intent,
            FakeSimulator(),
            minimum_amount_raw=10,
            max_attempts=4,
        )
    )

    assert requested == [100, 50]
    assert receipt.prepared.input_amount_raw == 50
    assert receipt.adaptive_attempts == 2
    assert len(receipt.adaptive_rejections) == 1
    assert "reported 1000" in receipt.adaptive_rejections[0]


def test_auto_seller_adaptive_preflight_stops_without_signing_unsafe_quote() -> None:
    requested: list[int] = []

    class FakeSigner:
        public_key = "Wallet111"

        def sign(self, transaction_b64: str) -> str:
            raise AssertionError("an unsafe quote must not be signed")

    class FakeClient:
        async def order(self, **values: object) -> dict[str, object]:
            amount = int(values["amount_raw"])
            requested.append(amount)
            return {
                "inputMint": values["input_mint"],
                "outputMint": USDC_MINT,
                "inAmount": str(amount),
                "outAmount": "10000000",
                "otherAmountThreshold": "9500000",
                "priceImpact": 1.0,
                "slippageBps": 1000,
                "transaction": f"unsigned-{amount}",
                "requestId": f"request-{amount}",
            }

    class FakeSimulator:
        async def simulate_transaction(
            self, signed_transaction_b64: str
        ) -> dict[str, object]:
            raise AssertionError("an unsafe quote must not be simulated")

    intent = SellIntent(
        mint="MintOwned111",
        symbol="OWN",
        stage=12,
        event_key="signal-exit:chunk:0",
        amount_raw=100,
        balance_raw=100,
        decimals=0,
        trigger_multiple=0,
        current_multiple=0,
        target_output_raw=None,
        reason="exit warning",
    )
    seller = SolanaAutoSeller(
        client=FakeClient(), signer=FakeSigner(), max_slippage_bps=500
    )

    with pytest.raises(ValueError, match="found no safe chunk"):
        asyncio.run(
            seller.preflight_adaptive(
                intent,
                FakeSimulator(),
                minimum_amount_raw=25,
                max_attempts=8,
            )
        )

    assert requested == [100, 50, 25]


def test_jupiter_preflight_order_excludes_rfq_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = JupiterSwapClient(api_key="key")
    requested_url = ""

    def fake_request(url: str, payload: object) -> dict[str, object]:
        nonlocal requested_url
        requested_url = url
        assert payload is None
        return {}

    monkeypatch.setattr(client, "_request_json", fake_request)

    asyncio.run(
        client.order(
            input_mint="MintProfit111",
            amount_raw=10,
            taker="Wallet111",
            exclude_routers=("jupiterz",),
        )
    )

    assert "excludeRouters=jupiterz" in requested_url


def test_jupiter_order_without_taker_omits_it_from_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reverse-quote callers (live canary/trial exit checks) omit taker,
    relying on OrderClient's optional taker; the concrete client must accept
    that instead of raising a missing-argument TypeError."""
    client = JupiterSwapClient(api_key="key")
    requested_url = ""

    def fake_request(url: str, payload: object) -> dict[str, object]:
        nonlocal requested_url
        requested_url = url
        assert payload is None
        return {}

    monkeypatch.setattr(client, "_request_json", fake_request)

    asyncio.run(
        client.order(
            input_mint="MintProfit111",
            output_mint=USDC_MINT,
            amount_raw=10,
        )
    )

    assert "taker=" not in requested_url


def test_jupiter_execute_sends_block_height_as_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = JupiterSwapClient(api_key="key")
    requested_payload: dict[str, object] = {}

    def fake_request(
        url: str, payload: dict[str, object] | None
    ) -> dict[str, object]:
        nonlocal requested_payload
        assert url.endswith("/execute")
        assert payload is not None
        requested_payload = payload
        return {"status": "Failed", "code": -1}

    monkeypatch.setattr(client, "_request_json", fake_request)

    asyncio.run(
        client.execute(
            signed_transaction="signed",
            request_id="request-1",
            last_valid_block_height="123456789",
        )
    )

    assert requested_payload["lastValidBlockHeight"] == "123456789"
    assert isinstance(requested_payload["lastValidBlockHeight"], str)

    with pytest.raises(ValueError, match="must be a decimal string"):
        asyncio.run(
            client.execute(
                signed_transaction="signed",
                request_id="request-2",
                last_valid_block_height=123456789,  # type: ignore[arg-type]
            )
        )


def test_jupiter_http_error_preserves_only_sanitized_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signature = "5" * 64
    signed_transaction = "A" * 500
    body = (
        "{"
        '"status":"Failed",'
        '"code":-2,'
        '"error":"invalid signed transaction private-api-key",'
        f'"signature":"{signature}",'
        f'"signedTransaction":"{signed_transaction}"'
        "}"
    ).encode()

    def reject(*_args: object, **_values: object) -> object:
        raise urllib.error.HTTPError(
            "https://api.jup.ag/swap/v2/execute",
            400,
            "Bad Request",
            None,
            io.BytesIO(body),
        )

    monkeypatch.setattr("urllib.request.urlopen", reject)
    client = JupiterSwapClient(api_key="private-api-key")

    with pytest.raises(JupiterRequestError) as caught:
        client._request_json(
            "https://api.jup.ag/swap/v2/execute",
            {
                "signedTransaction": signed_transaction,
                "requestId": "request-1",
            },
        )

    assert caught.value.http_status == 400
    assert caught.value.code == -2
    assert caught.value.signature == signature
    assert "invalid signed transaction" in str(caught.value)
    assert "[redacted-api-key]" in str(caught.value)
    assert signed_transaction not in str(caught.value)
    assert "private-api-key" not in str(caught.value)


def test_auto_seller_failure_preserves_jupiter_signature() -> None:
    class FakeSigner:
        public_key = "Wallet111"

        def sign(self, transaction_b64: str) -> str:
            assert transaction_b64 == "unsigned"
            return "signed"

    class FakeClient:
        async def execute(self, **_values: object) -> dict[str, object]:
            return {
                "status": "Failed",
                "code": -1,
                "signature": "failed-signature",
                "error": "request expired",
            }

    intent = SellIntent(
        mint="MintOwned111",
        symbol="OWN",
        stage=12,
        event_key="event-1",
        amount_raw=100,
        balance_raw=100,
        decimals=0,
        trigger_multiple=0,
        current_multiple=0,
        target_output_raw=None,
        reason="exit warning",
    )
    prepared = PreparedSell(
        intent=intent,
        transaction="unsigned",
        request_id="request-1",
        input_amount_raw=100,
        expected_output_raw=10,
        minimum_output_raw=9,
        price_impact_pct=1,
        last_valid_block_height="123",
    )
    seller = SolanaAutoSeller(client=FakeClient(), signer=FakeSigner())

    with pytest.raises(JupiterExecutionError) as caught:
        asyncio.run(seller.execute(prepared))

    assert caught.value.code == -1
    assert caught.value.signature == "failed-signature"


def test_auto_buyer_prepares_executes_and_uses_usdc() -> None:
    class FakeSigner:
        public_key = "Wallet111"

        def sign(self, transaction_b64: str) -> str:
            assert transaction_b64 == "unsigned-buy"
            return "signed-buy"

    class FakeClient:
        async def order(self, **values: object) -> dict[str, object]:
            assert values["input_mint"] == USDC_MINT
            assert values["output_mint"] == "MintBuy111"
            assert values["amount_raw"] == 5_000_000
            return {
                "inputMint": USDC_MINT,
                "outputMint": "MintBuy111",
                "inAmount": "5000000",
                "outAmount": "250000000",
                "otherAmountThreshold": "240000000",
                "priceImpact": -0.5,
                "transaction": "unsigned-buy",
                "requestId": "request-buy",
                "lastValidBlockHeight": "321",
            }

        async def execute(self, **values: object) -> dict[str, object]:
            assert values["signed_transaction"] == "signed-buy"
            assert values["last_valid_block_height"] == "321"
            return {
                "status": "Success",
                "code": 0,
                "signature": "signature-buy",
                "totalInputAmount": "5000000",
                "totalOutputAmount": "249000000",
            }

    intent = BuyIntent(
        mint="MintBuy111",
        symbol="BUY",
        event_key="buy-event",
        amount_usdc_raw=5_000_000,
        funding_source="seed",
    )
    buyer = SolanaAutoBuyer(client=FakeClient(), signer=FakeSigner())
    prepared = asyncio.run(buyer.prepare(intent))
    receipt = asyncio.run(buyer.execute(prepared))

    assert isinstance(prepared, PreparedBuy)
    assert prepared.minimum_output_raw == 240_000_000
    assert receipt.signature == "signature-buy"
    assert receipt.output_amount_raw == 249_000_000


def test_auto_buyer_preflight_never_executes() -> None:
    executed = False

    class FakeSigner:
        public_key = "Wallet111"

        def sign(self, transaction_b64: str) -> str:
            assert transaction_b64 == "unsigned-buy"
            return "signed-buy"

    class FakeClient:
        async def order(self, **values: object) -> dict[str, object]:
            assert values["exclude_routers"] == ("jupiterz",)
            return {
                "inputMint": USDC_MINT,
                "outputMint": "MintBuy111",
                "inAmount": "5000000",
                "outAmount": "250000000",
                "otherAmountThreshold": "240000000",
                "priceImpact": 0.25,
                "transaction": "unsigned-buy",
                "requestId": "request-buy",
            }

        async def execute(self, **_values: object) -> dict[str, object]:
            nonlocal executed
            executed = True
            raise AssertionError("buy preflight must never execute")

    class FakeSimulator:
        async def simulate_transaction(
            self, signed_transaction_b64: str
        ) -> dict[str, object]:
            assert signed_transaction_b64 == "signed-buy"
            return {"err": None, "logs": ["one"], "unitsConsumed": 99}

    intent = BuyIntent(
        mint="MintBuy111",
        symbol="BUY",
        event_key="buy-preflight",
        amount_usdc_raw=5_000_000,
        funding_source="seed",
    )
    buyer = SolanaAutoBuyer(client=FakeClient(), signer=FakeSigner())
    receipt = asyncio.run(buyer.preflight(intent, FakeSimulator()))

    assert receipt.broadcast is False
    assert receipt.units_consumed == 99
    assert receipt.log_count == 1
    assert executed is False


def test_auto_buyer_rejects_unsafe_price_impact() -> None:
    class FakeSigner:
        public_key = "Wallet111"

        def sign(self, transaction_b64: str) -> str:
            return transaction_b64

    class FakeClient:
        async def order(self, **values: object) -> dict[str, object]:
            return {
                "inputMint": USDC_MINT,
                "outputMint": values["output_mint"],
                "inAmount": str(values["amount_raw"]),
                "outAmount": "1",
                "otherAmountThreshold": "1",
                "priceImpact": -5.01,
                "transaction": "unsigned-buy",
                "requestId": "request-buy",
            }

        async def execute(self, **_values: object) -> dict[str, object]:
            raise AssertionError("unsafe buy must never execute")

    intent = BuyIntent(
        mint="MintBuy111",
        symbol="BUY",
        event_key="buy-impact",
        amount_usdc_raw=5_000_000,
        funding_source="seed",
    )
    buyer = SolanaAutoBuyer(
        client=FakeClient(), signer=FakeSigner(), max_price_impact_pct=5
    )

    with pytest.raises(ValueError, match="price impact"):
        asyncio.run(buyer.prepare(intent))


def test_auto_buyer_rejects_jupiter_twenty_percent_slippage() -> None:
    class FakeSigner:
        public_key = "Wallet111"

        def sign(self, transaction_b64: str) -> str:
            return transaction_b64

    class FakeClient:
        async def order(self, **values: object) -> dict[str, object]:
            return {
                "inputMint": USDC_MINT,
                "outputMint": values["output_mint"],
                "inAmount": str(values["amount_raw"]),
                "outAmount": "250000000",
                "otherAmountThreshold": "200000000",
                "priceImpact": -2.7,
                "slippageBps": 2000,
                "transaction": "unsigned-buy",
                "requestId": "request-buy",
            }

        async def execute(self, **_values: object) -> dict[str, object]:
            raise AssertionError("unsafe buy must never execute")

    intent = BuyIntent(
        mint="MintBuy111",
        symbol="BUY",
        event_key="buy-slippage",
        amount_usdc_raw=5_000_000,
        funding_source="seed",
    )
    buyer = SolanaAutoBuyer(
        client=FakeClient(), signer=FakeSigner(), max_slippage_bps=500
    )

    with pytest.raises(ValueError, match="slippage 2000 bps"):
        asyncio.run(buyer.prepare(intent))


def test_auto_buyer_can_floor_guard_percentages_when_opted_in() -> None:
    class FakeSigner:
        public_key = "Wallet111"

        def sign(self, transaction_b64: str) -> str:
            return transaction_b64

    class FakeClient:
        async def order(self, **values: object) -> dict[str, object]:
            return {
                "inputMint": USDC_MINT,
                "outputMint": values["output_mint"],
                "inAmount": str(values["amount_raw"]),
                "outAmount": "250000000",
                "otherAmountThreshold": "237500000",
                "priceImpact": 5.03,
                "slippageBps": 503,
                "transaction": "unsigned-buy",
                "requestId": "request-floored-buy",
            }

    intent = BuyIntent(
        mint="MintBuy111",
        symbol="BUY",
        event_key="buy-floored",
        amount_usdc_raw=5_000_000,
        funding_source="seed",
    )
    buyer = SolanaAutoBuyer(
        client=FakeClient(),
        signer=FakeSigner(),
        max_price_impact_pct=5,
        max_slippage_bps=500,
        floor_percentages=True,
    )

    prepared = asyncio.run(buyer.prepare(intent))

    assert prepared.price_impact_pct == 5
    assert prepared.quoted_price_impact_pct == 5.03
    assert prepared.slippage_bps == 500
    assert prepared.quoted_slippage_bps == 503


def test_auto_seller_rejects_second_stage_quote_below_trigger() -> None:
    class FakeSigner:
        public_key = "Wallet111"

        def sign(self, transaction_b64: str) -> str:
            return transaction_b64

    class FakeClient:
        async def order(self, **values: object) -> dict[str, object]:
            return {
                "inputMint": values["input_mint"],
                "outputMint": (
                    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
                ),
                "inAmount": str(values["amount_raw"]),
                "outAmount": "74000000",
                "otherAmountThreshold": "73000000",
                "transaction": "unsigned",
                "requestId": "request-2",
            }

        async def execute(self, **_values: object) -> dict[str, object]:
            raise AssertionError("an unsafe quote must never execute")

    intent = SellIntent(
        mint="MintProfit111",
        symbol="WIN",
        stage=1,
        event_key="event-2",
        amount_raw=25_000_000,
        balance_raw=50_000_000,
        decimals=6,
        trigger_multiple=3,
        current_multiple=3.1,
        target_output_raw=75_000_000,
        reason="sell half",
    )
    seller = SolanaAutoSeller(client=FakeClient(), signer=FakeSigner())

    with pytest.raises(ValueError, match="below the 3x trigger value"):
        asyncio.run(seller.prepare(intent))


def test_auto_sell_store_is_armed_and_idempotent(tmp_path: Path) -> None:
    store = SQLiteStore(str(tmp_path / "auto-sell.db"))
    store.save_owned_holding(
        OwnedHolding(
            chain="solana",
            token_address="MintProfit111",
            symbol="WIN",
            quantity=100,
            entry_price=1,
            price_currency="USD",
            cost_amount=100,
        )
    )
    store.arm_auto_sell("MintProfit111")
    policy = store.load_auto_sell_policy("MintProfit111")
    assert policy is not None
    assert policy["armed"] == 1
    assert policy["stage"] == 0

    values = {
        "event_key": "event-1",
        "chain": "solana",
        "token_address": "MintProfit111",
        "symbol": "WIN",
        "stage": 0,
        "requested_raw": 50,
        "expected_output_raw": 100,
    }
    assert store.begin_auto_sell_execution(**values) is True
    assert store.begin_auto_sell_execution(**values) is False
    store.complete_auto_sell_execution(
        event_key="event-1", signature="signature-1", next_stage=1
    )
    assert store.load_auto_sell_policy("MintProfit111")["stage"] == 1
    store.close()


def test_auto_sell_batch_persists_confirmed_chunk_progress(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(str(tmp_path / "sell-batch.db"))
    batch_values = {
        "batch_key": "solana:MintOwned111:portfolio-signal:exit-warning",
        "chain": "solana",
        "token_address": "MintOwned111",
        "symbol": "OWN",
        "stage": 12,
        "target_raw": 100,
        "full_exit": True,
    }
    batch = store.load_or_create_auto_sell_batch(**batch_values)
    assert batch["status"] == "ACTIVE"
    assert batch["sold_raw"] == 0

    first_key = f"{batch_values['batch_key']}:chunk:0"
    assert store.begin_auto_sell_execution(
        event_key=first_key,
        chain="solana",
        token_address="MintOwned111",
        symbol="OWN",
        stage=12,
        requested_raw=40,
        expected_output_raw=4_000_000,
    )
    assert not store.complete_auto_sell_chunk(
        batch_key=str(batch_values["batch_key"]),
        event_key=first_key,
        signature="sig-1",
        sold_raw=40,
        output_usdc_raw=4_000_000,
    )
    batch = store.load_or_create_auto_sell_batch(**batch_values)
    assert batch["sold_raw"] == 40
    assert batch["next_chunk_index"] == 1
    assert batch["status"] == "ACTIVE"
    assert batch["proceeds_usdc_raw"] == 4_000_000

    second_key = f"{batch_values['batch_key']}:chunk:1"
    assert store.begin_auto_sell_execution(
        event_key=second_key,
        chain="solana",
        token_address="MintOwned111",
        symbol="OWN",
        stage=12,
        requested_raw=60,
        expected_output_raw=6_000_000,
    )
    assert store.complete_auto_sell_chunk(
        batch_key=str(batch_values["batch_key"]),
        event_key=second_key,
        signature="sig-2",
        sold_raw=60,
        output_usdc_raw=6_000_000,
    )
    batch = store.load_or_create_auto_sell_batch(**batch_values)
    assert batch["sold_raw"] == 100
    assert batch["next_chunk_index"] == 2
    assert batch["status"] == "CONFIRMED"
    assert batch["last_signature"] == "sig-2"
    assert batch["proceeds_usdc_raw"] == 10_000_000
    store.close()


def test_auto_sell_batch_migration_adds_proceeds_column(
    tmp_path: Path,
) -> None:
    path = tmp_path / "old-sell-batch.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE auto_sell_batches (
            batch_key TEXT PRIMARY KEY,
            chain TEXT NOT NULL,
            token_address TEXT NOT NULL,
            symbol TEXT NOT NULL,
            stage INTEGER NOT NULL,
            status TEXT NOT NULL,
            target_raw INTEGER NOT NULL,
            full_exit INTEGER NOT NULL DEFAULT 0,
            sold_raw INTEGER NOT NULL DEFAULT 0,
            next_chunk_index INTEGER NOT NULL DEFAULT 0,
            last_signature TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.commit()
    connection.close()

    store = SQLiteStore(str(path))
    columns = {
        row["name"]
        for row in store.connection.execute(
            "PRAGMA table_info(auto_sell_batches)"
        ).fetchall()
    }
    assert "proceeds_usdc_raw" in columns
    store.close()


def test_auto_sell_batch_freezes_after_uncertain_execution(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(str(tmp_path / "sell-batch-review.db"))
    batch_values = {
        "batch_key": "solana:MintOwned111:portfolio-signal:exit-warning",
        "chain": "solana",
        "token_address": "MintOwned111",
        "symbol": "OWN",
        "stage": 12,
        "target_raw": 100,
        "full_exit": True,
    }
    store.load_or_create_auto_sell_batch(**batch_values)
    event_key = f"{batch_values['batch_key']}:chunk:0"
    assert store.begin_auto_sell_execution(
        event_key=event_key,
        chain="solana",
        token_address="MintOwned111",
        symbol="OWN",
        stage=12,
        requested_raw=40,
        expected_output_raw=4_000_000,
    )

    store.freeze_auto_sell_chunk(
        batch_key=str(batch_values["batch_key"]),
        event_key=event_key,
        error="confirmation unavailable",
    )

    batch = store.load_or_create_auto_sell_batch(**batch_values)
    execution = store.connection.execute(
        "SELECT status, error FROM auto_sell_executions WHERE event_key = ?",
        (event_key,),
    ).fetchone()
    assert batch["status"] == "REVIEW"
    assert batch["error"] == "confirmation unavailable"
    assert execution["status"] == "REVIEW"
    assert execution["error"] == "confirmation unavailable"
    store.close()


def test_auto_sell_review_requires_balance_match_then_pauses_before_resume(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(str(tmp_path / "sell-review-resolution.db"))
    batch_key = "solana:MintOwned111:portfolio-signal:exit-warning"
    batch_values = {
        "batch_key": batch_key,
        "chain": "solana",
        "token_address": "MintOwned111",
        "symbol": "OWN",
        "stage": 12,
        "target_raw": 100,
        "full_exit": True,
    }
    store.load_or_create_auto_sell_batch(**batch_values)
    event_key = f"{batch_key}:chunk:0"
    assert store.begin_auto_sell_execution(
        event_key=event_key,
        chain="solana",
        token_address="MintOwned111",
        symbol="OWN",
        stage=12,
        requested_raw=100,
        expected_output_raw=4_000_000,
        balance_before_raw=100,
    )
    store.freeze_auto_sell_chunk(
        batch_key=batch_key,
        event_key=event_key,
        error="HTTP 400: request expired",
    )

    reviews = store.auto_sell_review_status()
    assert reviews["batches"][0]["status"] == "REVIEW"
    assert reviews["executions"][0]["balance_before_raw"] == 100
    with pytest.raises(ValueError, match="explicit no-transaction"):
        store.resolve_auto_sell_review(
            batch_key=batch_key,
            confirmed_no_transaction=False,
            verified_balance_raw=100,
        )
    with pytest.raises(ValueError, match="current on-chain balance differs"):
        store.resolve_auto_sell_review(
            batch_key=batch_key,
            confirmed_no_transaction=True,
            verified_balance_raw=99,
        )

    batch = store.resolve_auto_sell_review(
        batch_key=batch_key,
        confirmed_no_transaction=True,
        verified_balance_raw=100,
    )
    assert batch["status"] == "PAUSED"
    assert batch["next_chunk_index"] == 1
    execution = store.load_auto_sell_review(batch_key)["execution"]
    assert execution["status"] == "CLEARED_NO_TRANSACTION"
    with pytest.raises(ValueError, match="stopped-monitor"):
        store.resume_auto_sell_batch(
            batch_key=batch_key, confirmed_monitor_stopped=False
        )

    batch = store.resume_auto_sell_batch(
        batch_key=batch_key, confirmed_monitor_stopped=True
    )
    assert batch["status"] == "ACTIVE"
    assert batch["next_chunk_index"] == 1
    assert store.begin_auto_sell_execution(
        event_key=f"{batch_key}:chunk:1",
        chain="solana",
        token_address="MintOwned111",
        symbol="OWN",
        stage=12,
        requested_raw=100,
        expected_output_raw=4_000_000,
        balance_before_raw=100,
    )
    store.close()


def test_auto_sell_review_with_signature_cannot_be_resolved(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(str(tmp_path / "sell-review-signature.db"))
    batch_key = "solana:MintOwned111:portfolio-signal:exit-warning"
    store.load_or_create_auto_sell_batch(
        batch_key=batch_key,
        chain="solana",
        token_address="MintOwned111",
        symbol="OWN",
        stage=12,
        target_raw=100,
        full_exit=True,
    )
    event_key = f"{batch_key}:chunk:0"
    assert store.begin_auto_sell_execution(
        event_key=event_key,
        chain="solana",
        token_address="MintOwned111",
        symbol="OWN",
        stage=12,
        requested_raw=100,
        expected_output_raw=4_000_000,
        balance_before_raw=100,
    )
    store.freeze_auto_sell_chunk(
        batch_key=batch_key,
        event_key=event_key,
        error="failed after submission",
        signature="possible-signature",
    )

    with pytest.raises(ValueError, match="inspect it on-chain"):
        store.resolve_auto_sell_review(
            batch_key=batch_key,
            confirmed_no_transaction=True,
            verified_balance_raw=100,
        )
    store.close()


def test_auto_sell_review_reconciliation_supports_legacy_full_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet = str(Keypair().pubkey())
    store = SQLiteStore(str(tmp_path / "sell-review-reconcile.db"))
    batch_key = "solana:MintOwned111:portfolio-signal:exit-warning"
    store.load_or_create_auto_sell_batch(
        batch_key=batch_key,
        chain="solana",
        token_address="MintOwned111",
        symbol="OWN",
        stage=12,
        target_raw=105_187_579_353,
        full_exit=True,
    )
    event_key = f"{batch_key}:chunk:0"
    assert store.begin_auto_sell_execution(
        event_key=event_key,
        chain="solana",
        token_address="MintOwned111",
        symbol="OWN",
        stage=12,
        requested_raw=105_187_579_353,
        expected_output_raw=1_000_000,
    )
    store.freeze_auto_sell_chunk(
        batch_key=batch_key,
        event_key=event_key,
        error="Jupiter request was rejected (HTTP 400)",
    )

    class FakeRpc:
        def __init__(self, _url: str) -> None:
            pass

        async def token_balance(
            self, owner: str, mint: str
        ) -> SolanaTokenHolding:
            assert owner == wallet
            assert mint == "MintOwned111"
            return SolanaTokenHolding(
                mint=mint,
                amount=105_187.579353,
                raw_amount=105_187_579_353,
                decimals=6,
            )

    monkeypatch.setattr("solana_launch_guard.app.SolanaRpc", FakeRpc)
    config = settings(
        tmp_path / "sell-review-reconcile.db",
        solana_wallet_address=wallet,
        auto_sell_enabled=True,
        auto_sell_live=False,
        auto_buy_live=False,
    )

    result = asyncio.run(
        reconcile_auto_sell_review(config, store, batch_key)
    )

    assert result["result"] == "BALANCE_UNCHANGED"
    assert result["balance_before_source"] == "inferred_full_exit_remainder"
    assert result["balance_unchanged"] is True
    assert result["eligible_to_resolve"] is True
    assert result["broadcast"] is False
    store.close()


def test_auto_sell_review_schema_migrates_from_v019(tmp_path: Path) -> None:
    database_path = tmp_path / "v019-review.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE auto_sell_executions (
            event_key TEXT PRIMARY KEY,
            chain TEXT NOT NULL,
            token_address TEXT NOT NULL,
            symbol TEXT NOT NULL,
            stage INTEGER NOT NULL,
            status TEXT NOT NULL,
            requested_raw INTEGER NOT NULL,
            expected_output_raw INTEGER,
            signature TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.commit()
    connection.close()

    store = SQLiteStore(str(database_path))
    columns = {
        str(row["name"])
        for row in store.connection.execute(
            "PRAGMA table_info(auto_sell_executions)"
        ).fetchall()
    }

    assert "balance_before_raw" in columns
    store.close()


def test_auto_sell_per_mint_block_works_without_cost_basis(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(str(tmp_path / "auto-sell-block.db"))
    store.disarm_auto_sell("MintUnknown111")
    policy = store.load_auto_sell_policy("MintUnknown111")
    assert policy is not None
    assert policy["armed"] == 0

    store.allow_auto_sell_signals("MintUnknown111")
    policy = store.load_auto_sell_policy("MintUnknown111")
    assert policy is not None
    assert policy["armed"] == 1
    store.close()


def test_auto_buy_store_uses_two_seed_buys_then_profit_pool(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(str(tmp_path / "auto-buy.db"))
    store.arm_auto_buy("MintBuyOne", "ONE")
    amount, source = store.preview_auto_buy_budget(
        seed_size_usdc_raw=5_000_000,
        max_seed_buys=2,
        max_open_positions=2,
    )
    assert (amount, source) == (5_000_000, "seed")
    assert store.begin_auto_buy_execution(
        event_key="buy-one",
        token_address="MintBuyOne",
        symbol="ONE",
        funding_source=source,
        input_usdc_raw=amount,
        expected_output_raw=1_000,
    )
    store.complete_auto_buy_execution(
        event_key="buy-one",
        signature="signature-buy-one",
        actual_output_raw=1_000,
        output_decimals=2,
    )
    realized = store.record_auto_buy_sale(
        token_address="MintBuyOne",
        sold_raw=1_000,
        proceeds_usdc_raw=8_000_000,
        reinvest_pct=50,
    )
    assert realized == {
        "allocated_cost_usdc_raw": 5_000_000,
        "profit_usdc_raw": 3_000_000,
        "reinvest_credit_usdc_raw": 1_500_000,
        "remaining_raw": 0,
    }

    store.arm_auto_buy("MintBuyTwo", "TWO")
    amount, source = store.preview_auto_buy_budget(
        seed_size_usdc_raw=5_000_000,
        max_seed_buys=2,
        max_open_positions=2,
    )
    assert source == "seed"
    assert store.begin_auto_buy_execution(
        event_key="buy-two",
        token_address="MintBuyTwo",
        symbol="TWO",
        funding_source=source,
        input_usdc_raw=amount,
        expected_output_raw=2_000,
    )
    store.complete_auto_buy_execution(
        event_key="buy-two",
        signature="signature-buy-two",
        actual_output_raw=2_000,
        output_decimals=2,
    )

    amount, source = store.preview_auto_buy_budget(
        seed_size_usdc_raw=5_000_000,
        max_seed_buys=2,
        max_open_positions=2,
    )
    assert (amount, source) == (1_500_000, "reinvested_profit")
    status = store.auto_buy_status()
    assert status["fund"]["seed_buys_used"] == 2
    assert status["fund"]["reinvest_available_usdc_raw"] == 1_500_000
    store.close()


def test_auto_buy_store_blocks_third_open_position(tmp_path: Path) -> None:
    store = SQLiteStore(str(tmp_path / "auto-buy-limit.db"))
    for number in (1, 2):
        mint = f"MintBuy{number}"
        store.arm_auto_buy(mint, f"BUY{number}")
        amount, source = store.preview_auto_buy_budget(
            seed_size_usdc_raw=5_000_000,
            max_seed_buys=2,
            max_open_positions=2,
        )
        store.begin_auto_buy_execution(
            event_key=f"buy-{number}",
            token_address=mint,
            symbol=f"BUY{number}",
            funding_source=source,
            input_usdc_raw=amount,
            expected_output_raw=100,
        )
        store.complete_auto_buy_execution(
            event_key=f"buy-{number}",
            signature=f"signature-{number}",
            actual_output_raw=100,
            output_decimals=0,
        )

    with pytest.raises(ValueError, match="maximum open"):
        store.preview_auto_buy_budget(
            seed_size_usdc_raw=5_000_000,
            max_seed_buys=2,
            max_open_positions=2,
        )
    store.close()


def test_key_store_refuses_mismatched_wallet_before_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keypair = Keypair()
    wrote_secret = False

    monkeypatch.setattr(
        "solana_launch_guard.execution.getpass.getpass",
        lambda _prompt: str(keypair),
    )

    def mark_write(*_values: object) -> None:
        nonlocal wrote_secret
        wrote_secret = True

    monkeypatch.setattr(
        "solana_launch_guard.execution.keyring.set_password", mark_write
    )

    with pytest.raises(ValueError, match="nothing was stored"):
        store_fomo_solana_key(expected_public_key=str(Keypair().pubkey()))

    assert wrote_secret is False


def test_preflight_command_uses_armed_balance_and_preserves_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet = str(Keypair().pubkey())
    store = SQLiteStore(str(tmp_path / "preflight.db"))
    store.save_owned_holding(
        OwnedHolding(
            chain="solana",
            token_address="MintProfit111",
            symbol="WIN",
            quantity=100,
            entry_price=1,
            price_currency="USD",
            cost_amount=100,
        )
    )
    store.arm_auto_sell("MintProfit111")

    class FakeRpc:
        def __init__(self, _url: str) -> None:
            pass

        async def token_holdings(
            self, owner: str
        ) -> tuple[SolanaTokenHolding, ...]:
            assert owner == wallet
            return (
                SolanaTokenHolding(
                    mint="MintProfit111",
                    amount=100,
                    raw_amount=100_000_000,
                    decimals=6,
                ),
            )

        async def simulate_transaction(
            self, signed_transaction_b64: str
        ) -> dict[str, object]:
            assert signed_transaction_b64 == "signed"
            return {"err": None, "logs": [], "unitsConsumed": 123}

    class FakeSigner:
        def __init__(self, *, expected_public_key: str) -> None:
            assert expected_public_key == wallet
            self.public_key = wallet

        def sign(self, transaction_b64: str) -> str:
            assert transaction_b64 == "unsigned"
            return "signed"

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            assert api_key == "jupiter-key"

        async def order(self, **values: object) -> dict[str, object]:
            assert values["exclude_routers"] == ("jupiterz",)
            return {
                "inputMint": values["input_mint"],
                "outputMint": (
                    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
                ),
                "inAmount": str(values["amount_raw"]),
                "outAmount": "10000000",
                "otherAmountThreshold": "9500000",
                "priceImpact": 0.1,
                "transaction": "unsigned",
                "requestId": "request-preflight",
            }

        async def execute(self, **_values: object) -> dict[str, object]:
            raise AssertionError("preflight must never execute")

    monkeypatch.setattr("solana_launch_guard.app.SolanaRpc", FakeRpc)
    monkeypatch.setattr("solana_launch_guard.app.KeyringSolanaSigner", FakeSigner)
    monkeypatch.setattr("solana_launch_guard.app.JupiterSwapClient", FakeClient)
    config = settings(
        tmp_path / "preflight.db",
        solana_wallet_address=wallet,
        jupiter_api_key="jupiter-key",
        auto_sell_enabled=True,
        auto_sell_live=False,
    )

    result = asyncio.run(
        preflight_auto_sell(config, store, "MintProfit111")
    )

    assert result["result"] == "PASSED"
    assert result["broadcast"] is False
    assert result["input_tokens"] == pytest.approx(50)
    assert result["simulation_units_consumed"] == 123
    policy = store.load_auto_sell_policy("MintProfit111")
    assert policy is not None
    assert policy["stage"] == 0
    store.close()


def test_owned_auto_sell_preflight_never_broadcasts_without_cost_basis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet = str(Keypair().pubkey())
    store = SQLiteStore(str(tmp_path / "owned-preflight.db"))

    class FakeRpc:
        def __init__(self, _url: str) -> None:
            pass

        async def token_holdings(
            self, owner: str
        ) -> tuple[SolanaTokenHolding, ...]:
            assert owner == wallet
            return (
                SolanaTokenHolding(
                    mint="MintUnknown111",
                    amount=100,
                    raw_amount=100_000_000,
                    decimals=6,
                ),
            )

        async def simulate_transaction(
            self, signed_transaction_b64: str
        ) -> dict[str, object]:
            assert signed_transaction_b64 == "signed"
            return {"err": None, "logs": ["ok"], "unitsConsumed": 321}

    class FakeSigner:
        def __init__(self, *, expected_public_key: str) -> None:
            assert expected_public_key == wallet
            self.public_key = wallet

        def sign(self, transaction_b64: str) -> str:
            assert transaction_b64 == "unsigned"
            return "signed"

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            assert api_key == "jupiter-key"

        async def order(self, **values: object) -> dict[str, object]:
            amount_raw = int(values["amount_raw"])
            assert amount_raw in {50_000_000, 25_000_000}
            return {
                "inputMint": "MintUnknown111",
                "outputMint": USDC_MINT,
                "inAmount": str(amount_raw),
                "outAmount": "10000000",
                "otherAmountThreshold": "9500000",
                "priceImpact": 5.03,
                "slippageBps": 1000 if amount_raw > 25_000_000 else 503,
                "transaction": "unsigned",
                "requestId": "owned-preflight",
            }

        async def execute(self, **_values: object) -> dict[str, object]:
            raise AssertionError("preflight must never execute")

    monkeypatch.setattr("solana_launch_guard.app.SolanaRpc", FakeRpc)
    monkeypatch.setattr("solana_launch_guard.app.KeyringSolanaSigner", FakeSigner)
    monkeypatch.setattr("solana_launch_guard.app.JupiterSwapClient", FakeClient)
    config = settings(
        tmp_path / "owned-preflight.db",
        solana_wallet_address=wallet,
        jupiter_api_key="jupiter-key",
        auto_sell_enabled=True,
        auto_sell_portfolio_signals=True,
        auto_trade_floor_percentages=True,
        auto_sell_adaptive_chunks=True,
    )

    result = asyncio.run(
        preflight_owned_auto_sell(config, store, "MintUnknown111")
    )

    assert result["result"] == "PASSED"
    assert result["broadcast"] is False
    assert result["configured_fraction"] == 0.5
    assert result["selected_fraction"] == 0.25
    assert result["adaptive_attempts"] == 2
    assert len(result["adaptive_rejections"]) == 1
    assert result["input_tokens"] == 25
    assert result["price_impact_pct"] == 5
    assert result["quoted_price_impact_pct"] == 5.03
    assert result["slippage_bps"] == 500
    assert result["quoted_slippage_bps"] == 503
    assert result["simulation_units_consumed"] == 321
    store.close()


def test_auto_buy_preflight_uses_seed_budget_without_broadcast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet = str(Keypair().pubkey())
    store = SQLiteStore(str(tmp_path / "buy-preflight.db"))
    store.arm_auto_buy("MintBuy111", "BUY")

    class FakeRpc:
        def __init__(self, _url: str) -> None:
            pass

        async def token_balance(
            self, owner: str, mint: str
        ) -> SolanaTokenHolding:
            assert owner == wallet
            if mint == USDC_MINT:
                return SolanaTokenHolding(
                    mint=mint,
                    amount=20,
                    raw_amount=20_000_000,
                    decimals=6,
                )
            return SolanaTokenHolding(
                mint=mint, amount=0, raw_amount=0, decimals=6
            )

        async def mint_decimals(self, mint: str) -> int:
            assert mint == "MintBuy111"
            return 6

        async def simulate_transaction(
            self, signed_transaction_b64: str
        ) -> dict[str, object]:
            assert signed_transaction_b64 == "signed-buy"
            return {"err": None, "logs": ["ok"], "unitsConsumed": 456}

    class FakeSigner:
        def __init__(self, *, expected_public_key: str) -> None:
            assert expected_public_key == wallet
            self.public_key = wallet

        def sign(self, transaction_b64: str) -> str:
            assert transaction_b64 == "unsigned-buy"
            return "signed-buy"

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            assert api_key == "jupiter-key"

        async def order(self, **values: object) -> dict[str, object]:
            assert values["input_mint"] == USDC_MINT
            assert values["output_mint"] == "MintBuy111"
            assert values["exclude_routers"] == ("jupiterz",)
            return {
                "inputMint": USDC_MINT,
                "outputMint": "MintBuy111",
                "inAmount": "5000000",
                "outAmount": "250000000",
                "otherAmountThreshold": "240000000",
                "priceImpact": -0.4,
                "transaction": "unsigned-buy",
                "requestId": "buy-preflight",
            }

        async def execute(self, **_values: object) -> dict[str, object]:
            raise AssertionError("preflight must never execute")

    monkeypatch.setattr("solana_launch_guard.app.SolanaRpc", FakeRpc)
    monkeypatch.setattr("solana_launch_guard.app.KeyringSolanaSigner", FakeSigner)
    monkeypatch.setattr("solana_launch_guard.app.JupiterSwapClient", FakeClient)
    config = settings(
        tmp_path / "buy-preflight.db",
        solana_wallet_address=wallet,
        jupiter_api_key="jupiter-key",
        auto_buy_enabled=True,
        auto_buy_live=False,
    )

    result = asyncio.run(preflight_auto_buy(config, store, "MintBuy111"))

    assert result["result"] == "PASSED"
    assert result["broadcast"] is False
    assert result["funding_source"] == "seed"
    assert result["input_usdc"] == 5
    assert result["expected_output_tokens"] == 250
    assert result["simulation_units_consumed"] == 456
    assert store.auto_buy_status()["fund"]["seed_buys_used"] == 0
    store.close()


def test_auto_rebuy_preflight_respects_recovery_size_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet = str(Keypair().pubkey())
    store = SQLiteStore(str(tmp_path / "rebuy-preflight.db"))
    watch = store.start_auto_rebuy_watch(
        token_address="MintRecovery111",
        symbol="RECOVER",
        sell_signature="sell-1",
        exit_price_usd=1,
        exit_liquidity_usd=100_000,
        sale_proceeds_usdc_raw=10_000_000,
        sold_at_epoch=time.time(),
        max_rebuys=1,
    )
    assert watch is not None

    class FakeRpc:
        def __init__(self, _url: str) -> None:
            pass

        async def token_balance(
            self, owner: str, mint: str
        ) -> SolanaTokenHolding:
            assert owner == wallet
            if mint == USDC_MINT:
                return SolanaTokenHolding(
                    mint=mint, amount=20, raw_amount=20_000_000, decimals=6
                )
            return SolanaTokenHolding(
                mint=mint, amount=0, raw_amount=0, decimals=6
            )

        async def mint_decimals(self, mint: str) -> int:
            assert mint == "MintRecovery111"
            return 6

        async def simulate_transaction(
            self, signed_transaction_b64: str
        ) -> dict[str, object]:
            assert signed_transaction_b64 == "signed-buy"
            return {"err": None, "logs": ["ok"], "unitsConsumed": 456}

    class FakeSigner:
        def __init__(self, *, expected_public_key: str) -> None:
            assert expected_public_key == wallet
            self.public_key = wallet

        def sign(self, transaction_b64: str) -> str:
            assert transaction_b64 == "unsigned-buy"
            return "signed-buy"

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            assert api_key == "jupiter-key"

        async def order(self, **values: object) -> dict[str, object]:
            assert values["input_mint"] == USDC_MINT
            assert values["output_mint"] == "MintRecovery111"
            assert values["amount_raw"] == 3_000_000
            assert values["exclude_routers"] == ("jupiterz",)
            return {
                "inputMint": USDC_MINT,
                "outputMint": "MintRecovery111",
                "inAmount": "3000000",
                "outAmount": "150000000",
                "otherAmountThreshold": "145000000",
                "priceImpact": -0.4,
                "transaction": "unsigned-buy",
                "requestId": "rebuy-preflight",
            }

        async def execute(self, **_values: object) -> dict[str, object]:
            raise AssertionError("preflight must never execute")

    monkeypatch.setattr("solana_launch_guard.app.SolanaRpc", FakeRpc)
    monkeypatch.setattr("solana_launch_guard.app.KeyringSolanaSigner", FakeSigner)
    monkeypatch.setattr("solana_launch_guard.app.JupiterSwapClient", FakeClient)
    config = settings(
        tmp_path / "rebuy-preflight.db",
        solana_wallet_address=wallet,
        jupiter_api_key="jupiter-key",
        auto_sell_enabled=True,
        auto_buy_enabled=True,
        auto_buy_seed_size_usdc=10,
        auto_rebuy_enabled=True,
        auto_rebuy_max_size_usdc=3,
    )

    result = asyncio.run(
        preflight_auto_rebuy(config, store, "MintRecovery111")
    )

    assert result["result"] == "PASSED"
    assert result["broadcast"] is False
    assert result["input_usdc"] == 3
    assert result["expected_output_tokens"] == 150
    assert result["watch_status"] == "WATCHING"
    assert store.auto_buy_status()["fund"]["seed_buys_used"] == 0
    store.close()


def test_solana_rpc_simulation_rejects_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rpc = SolanaRpc("https://example.invalid")
    monkeypatch.setattr(
        rpc,
        "_request",
        lambda method, params: {
            "value": {"err": {"InstructionError": [2, "Custom"]}}
        },
    )

    with pytest.raises(RuntimeError, match="simulation failed"):
        asyncio.run(rpc.simulate_transaction("signed"))


def settings(database_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "ws_url": "wss://example.invalid/api/data",
        "api_key": None,
        "trade_size_sol": 0.02,
        "max_open_positions": 3,
        "max_total_exposure_sol": 0.06,
        "min_virtual_sol": 5.0,
        "min_market_cap_sol": 5.0,
        "max_market_cap_sol": 500.0,
        "max_creator_buy_sol": 5.0,
        "take_profit_pct": 30.0,
        "stop_loss_pct": 20.0,
        "reject_unknown_price": True,
        "database_path": str(database_path),
        "log_level": "INFO",
    }
    values.update(overrides)
    result = Settings(**values)
    result.validate()
    return result


def test_live_auto_buy_requires_live_auto_sell(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires AUTO_SELL_LIVE"):
        settings(
            tmp_path / "settings.db",
            solana_wallet_address=str(Keypair().pubkey()),
            jupiter_api_key="jupiter-key",
            auto_buy_enabled=True,
            auto_buy_live=True,
            auto_sell_enabled=True,
            auto_sell_live=False,
        )


def launch_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "mint": "Mint111",
        "name": "Example",
        "symbol": "EX",
        "txType": "create",
        "traderPublicKey": "Creator111",
        "signature": "Signature111",
        "vTokensInBondingCurve": 1_000_000,
        "vSolInBondingCurve": 30,
        "marketCapSol": 30,
        "solAmount": 1,
    }
    payload.update(overrides)
    return payload


def test_indicative_price_uses_virtual_reserves() -> None:
    assert indicative_price(launch_payload()) == pytest.approx(0.00003)


def test_risk_engine_accepts_event_inside_limits(tmp_path: Path) -> None:
    config = settings(tmp_path / "test.db")
    store = SQLiteStore(config.database_path)
    broker = PaperBroker(config, store)

    decision = RiskEngine(config).evaluate(
        Launch.from_payload(launch_payload()), broker
    )

    assert decision.accepted is True
    assert decision.reasons == ()
    # The score cannot imply full verification because on-chain enrichment
    # is deliberately not present in version 1.
    assert decision.score < 100
    store.close()


def test_risk_engine_rejects_large_creator_buy(tmp_path: Path) -> None:
    config = settings(tmp_path / "test.db")
    store = SQLiteStore(config.database_path)
    broker = PaperBroker(config, store)

    decision = RiskEngine(config).evaluate(
        Launch.from_payload(launch_payload(solAmount=10)), broker
    )

    assert decision.accepted is False
    assert any("creator buy" in reason for reason in decision.reasons)
    store.close()


def test_candidate_risk_ignores_full_paper_portfolio(tmp_path: Path) -> None:
    config = settings(
        tmp_path / "test.db",
        max_open_positions=1,
        max_total_exposure_sol=0.02,
    )
    store = SQLiteStore(config.database_path)
    broker = PaperBroker(config, store)
    broker.open(Launch.from_payload(launch_payload(mint="MintOne")))

    candidate = Launch.from_payload(launch_payload(mint="MintTwo"))
    assert RiskEngine(config).evaluate(candidate, broker).accepted is False
    assert RiskEngine(config).evaluate_candidate(candidate).accepted is True
    store.close()


def test_paper_position_hits_take_profit_and_is_persisted(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path / "test.db")
    store = SQLiteStore(config.database_path)
    broker = PaperBroker(config, store)
    launch = Launch.from_payload(launch_payload())

    position = broker.open(launch)
    broker.mark(launch.mint, position.entry_price_sol * 1.31)

    assert position.status == "CLOSED"
    assert position.exit_reason == "TAKE_PROFIT"
    assert position.pnl_pct == pytest.approx(31.0)
    assert store.summary()["realized_pnl_sol"] > 0
    store.close()


def test_exposure_limit_rejects_next_position(tmp_path: Path) -> None:
    config = settings(
        tmp_path / "test.db",
        max_open_positions=5,
        max_total_exposure_sol=0.02,
    )
    store = SQLiteStore(config.database_path)
    broker = PaperBroker(config, store)
    first = Launch.from_payload(launch_payload(mint="MintOne"))
    second = Launch.from_payload(launch_payload(mint="MintTwo"))

    broker.open(first)
    decision = RiskEngine(config).evaluate(second, broker)

    assert decision.accepted is False
    assert "maximum total exposure would be exceeded" in decision.reasons
    store.close()


def test_wallet_transaction_parser_detects_token_buy() -> None:
    wallet = "Wallet1111111111111111111111111111111111111"
    mint = "Mint222222222222222222222222222222222222222"
    transaction = {
        "transaction": {
            "message": {
                "accountKeys": [{"pubkey": wallet}, {"pubkey": "Program111"}]
            }
        },
        "meta": {
            "preBalances": [2_000_000_000, 0],
            "postBalances": [1_899_995_000, 0],
            "preTokenBalances": [
                {
                    "owner": wallet,
                    "mint": mint,
                    "uiTokenAmount": {"uiAmountString": "100", "decimals": 6},
                }
            ],
            "postTokenBalances": [
                {
                    "owner": wallet,
                    "mint": mint,
                    "uiTokenAmount": {"uiAmountString": "150", "decimals": 6},
                }
            ],
        },
    }

    trades = parse_wallet_trades(transaction, wallet, "Signature111", 123)

    assert len(trades) == 1
    assert trades[0].side == "BUY"
    assert trades[0].mint == mint
    assert trades[0].token_delta == pytest.approx(50)
    assert trades[0].native_sol_delta == pytest.approx(-0.100005)


def test_solana_rpc_reads_and_aggregates_owned_token_balances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rpc = SolanaRpc("https://example.invalid")

    def fake_request(_method: str, params: list[object]) -> object:
        options = params[1] if isinstance(params[1], dict) else {}
        program = str(options.get("programId"))
        amount = "1.5" if program.startswith("Tokenkeg") else "2.5"
        return {
            "value": [
                {
                    "account": {
                        "data": {
                            "parsed": {
                                "info": {
                                    "mint": "MintOwned111",
                                    "tokenAmount": {"uiAmountString": amount},
                                }
                            }
                        }
                    }
                }
            ]
        }

    monkeypatch.setattr(rpc, "_request", fake_request)
    holdings = asyncio.run(rpc.token_holdings("Wallet111"))

    assert len(holdings) == 1
    assert holdings[0].mint == "MintOwned111"
    assert holdings[0].amount == pytest.approx(4)


def market_quote(
    *,
    liquidity: float,
    market_cap: float,
    buys: int,
    sells: int,
    volume: float,
    change: float,
) -> MarketQuote:
    return MarketQuote(
        mint="MintScore111",
        symbol="SCORE",
        price_sol=0.000001,
        liquidity_usd=liquidity,
        market_cap_usd=market_cap,
        pair_address="Pair111",
        pair_created_at_ms=1,
        buys_m5=buys,
        sells_m5=sells,
        volume_m5_usd=volume,
        price_change_m5_pct=change,
    )


def test_intelligence_assigns_core_tier() -> None:
    result = CoinIntelligence().score(
        market_quote(
            liquidity=50_000,
            market_cap=100_000,
            buys=60,
            sells=20,
            volume=15_000,
            change=15,
        )
    )

    assert result.tier == "CORE"
    assert result.safety_score >= 35
    assert result.total_score >= 75


def test_intelligence_assigns_five_dollar_moonshot_tier() -> None:
    result = CoinIntelligence().score(
        market_quote(
            liquidity=10_000,
            market_cap=80_000,
            buys=25,
            sells=5,
            volume=10_000,
            change=80,
        )
    )

    assert result.tier == "MOONSHOT"
    assert result.total_score >= 60


def test_intelligence_hard_rejects_thin_liquidity() -> None:
    result = CoinIntelligence().score(
        market_quote(
            liquidity=1_000,
            market_cap=20_000,
            buys=100,
            sells=30,
            volume=50_000,
            change=100,
        )
    )

    assert result.tier == "REJECT"
    assert result.total_score == 0


def test_recommendations_rank_with_bounded_live_momentum() -> None:
    intelligence = CoinIntelligence()
    quote_a = market_quote(
        liquidity=50_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=15,
    )
    quote_b = MarketQuote(
        mint="MintScore222",
        symbol="FAST",
        price_sol=0.000001,
        liquidity_usd=50_000,
        market_cap_usd=100_000,
        pair_address="Pair222",
        pair_created_at_ms=1,
        buys_m5=60,
        sells_m5=20,
        volume_m5_usd=15_000,
        price_change_m5_pct=15,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60)
    book.add(quote_a, intelligence.score(quote_a), now=0)
    book.add(quote_b, intelligence.score(quote_b), now=0)
    book.update(
        MarketQuote(
            mint=quote_b.mint,
            symbol=quote_b.symbol,
            price_sol=0.0000012,
            liquidity_usd=quote_b.liquidity_usd,
            market_cap_usd=quote_b.market_cap_usd,
            pair_address=quote_b.pair_address,
            pair_created_at_ms=quote_b.pair_created_at_ms,
            buys_m5=quote_b.buys_m5,
            sells_m5=quote_b.sells_m5,
            volume_m5_usd=quote_b.volume_m5_usd,
            price_change_m5_pct=20,
        ),
        now=5,
    )

    ranked = book.ranked()

    assert ranked[0].mint == "MintScore222"
    assert ranked[0].rise_pct == pytest.approx(20)
    output = format_recommendations(ranked, color=True)
    assert "\033[38;5;220m" in output
    assert "mint=MintScore222" in output


def test_recommendations_expire_old_candidates() -> None:
    quote = market_quote(
        liquidity=50_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=15,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=10)
    book.add(quote, CoinIntelligence().score(quote), now=0)
    book.expire(now=11)
    assert book.ranked() == []


def test_recommendations_deduplicate_repeated_symbol() -> None:
    first = market_quote(
        liquidity=50_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=15,
    )
    second = MarketQuote(
        mint="DifferentMintSameSymbol",
        symbol=first.symbol.lower(),
        price_sol=first.price_sol,
        liquidity_usd=first.liquidity_usd,
        market_cap_usd=first.market_cap_usd,
        pair_address="PairDuplicate",
        pair_created_at_ms=first.pair_created_at_ms,
        buys_m5=first.buys_m5,
        sells_m5=first.sells_m5,
        volume_m5_usd=first.volume_m5_usd,
        price_change_m5_pct=first.price_change_m5_pct,
    )
    intelligence = CoinIntelligence()
    book = RecommendationBook(pool_size=10, ttl_seconds=60)
    book.add(first, intelligence.score(first), now=0)
    book.add(second, intelligence.score(second), now=1)

    ranked = book.ranked(limit=10)

    assert len(ranked) == 1
    assert ranked[0].symbol.casefold() == "score"


def test_snapshot_round_trip_and_colored_dashboard(tmp_path: Path) -> None:
    quote = market_quote(
        liquidity=50_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=15,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60)
    book.add(quote, CoinIntelligence().score(quote), now=0)
    snapshot = build_snapshot(book.ranked(), pending_count=4, poll_seconds=15)
    path = tmp_path / "recommendations.json"

    write_snapshot(path, snapshot)
    restored = read_snapshot(path)

    assert restored is not None
    assert restored["pending_count"] == 4
    assert len(restored["candidates"]) == 1
    output = format_dashboard(restored, color=True)
    assert "LAUNCH GUARD — READ-ONLY DECISION SUPPORT" in output
    assert "decision=WAIT FOR PULLBACK" in output
    assert "preferred entry=" in output
    assert "Pending scans: 4" in output
    assert "mint=MintScore111" in output
    assert "\033[38;5;45m" in output


def test_pullback_zone_is_anchored_and_alerts_once() -> None:
    initial = market_quote(
        liquidity=50_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=15,
    )
    book = RecommendationBook(
        pool_size=10, ttl_seconds=60, entry_confirmation_polls=1
    )
    candidate = book.add(initial, CoinIntelligence().score(initial), now=0)

    assert candidate is not None
    assert candidate.decision == "WAIT FOR PULLBACK"
    assert candidate.entry_zone_low == pytest.approx(initial.price_sol * 0.94)
    assert candidate.entry_zone_high == pytest.approx(initial.price_sol * 0.96)

    touches_low = MarketQuote(
        mint=initial.mint,
        symbol=initial.symbol,
        price_sol=initial.price_sol * 0.94,
        liquidity_usd=initial.liquidity_usd,
        market_cap_usd=initial.market_cap_usd,
        pair_address=initial.pair_address,
        pair_created_at_ms=initial.pair_created_at_ms,
        buys_m5=25,
        sells_m5=25,
        volume_m5_usd=16_000,
        price_change_m5_pct=-3,
    )
    book.update(touches_low, now=3)

    # A single touch of the zone is not itself a BUY - Launch Guard needs to
    # see price actually bounce off a tracked low before confirming.
    assert candidate.decision == "WATCH"
    assert candidate.pullback_low_price == pytest.approx(initial.price_sol * 0.94)

    pullback = MarketQuote(
        mint=initial.mint,
        symbol=initial.symbol,
        price_sol=initial.price_sol * 0.96,
        liquidity_usd=initial.liquidity_usd,
        market_cap_usd=initial.market_cap_usd,
        pair_address=initial.pair_address,
        pair_created_at_ms=initial.pair_created_at_ms,
        buys_m5=40,
        sells_m5=20,
        volume_m5_usd=18_000,
        price_change_m5_pct=2,
    )
    book.update(pullback, now=5)

    assert candidate.decision == "BUY ZONE"
    assert candidate.volume_label == "RISING"
    alerts = book.pop_buy_zone_alerts()
    assert alerts == [candidate]
    assert book.pop_buy_zone_alerts() == []

    output = format_dashboard(
        build_snapshot(
            book.ranked(),
            pending_count=0,
            poll_seconds=15,
            alerts=alerts,
        ),
        color=False,
    )
    assert "BUY ZONE ALERT" in output
    assert "decision=BUY ZONE" in output


def test_pullback_zone_without_a_bounce_never_confirms() -> None:
    """A price that drifts sideways/down inside the zone must not BUY ZONE,
    even with a superficially positive single-poll reading, until it has
    actually bounced off a tracked low."""
    initial = market_quote(
        liquidity=50_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=15,
    )
    book = RecommendationBook(
        pool_size=10, ttl_seconds=60, entry_confirmation_polls=1
    )
    candidate = book.add(initial, CoinIntelligence().score(initial), now=0)
    assert candidate is not None

    def quote_at(price_multiplier: float, change_pct: float) -> MarketQuote:
        return MarketQuote(
            mint=initial.mint,
            symbol=initial.symbol,
            price_sol=initial.price_sol * price_multiplier,
            liquidity_usd=initial.liquidity_usd,
            market_cap_usd=initial.market_cap_usd,
            pair_address=initial.pair_address,
            pair_created_at_ms=initial.pair_created_at_ms,
            buys_m5=40,
            sells_m5=20,
            volume_m5_usd=18_000,
            price_change_m5_pct=change_pct,
        )

    # First touch of the zone: nothing to bounce off yet.
    book.update(quote_at(0.95, 1), now=3)
    assert candidate.decision == "WATCH"
    assert candidate.pullback_low_price == pytest.approx(
        initial.price_sol * 0.95
    )

    # Drifts to a new, lower low inside the zone - still no bounce.
    book.update(quote_at(0.94, 1), now=6)
    assert candidate.decision == "WATCH"
    assert candidate.pullback_low_price == pytest.approx(
        initial.price_sol * 0.94
    )

    # Ticks back up, but only ~1.1% above the *new* (lower) tracked low -
    # not enough of a bounce to confirm yet.
    book.update(quote_at(0.95, 1), now=9)
    assert candidate.decision == "WATCH"
    assert candidate.pullback_low_price == pytest.approx(
        initial.price_sol * 0.94
    )

    # A real bounce (~2.1%) off the tracked low finally confirms.
    book.update(quote_at(0.96, 1), now=12)
    assert candidate.decision == "BUY ZONE"


def test_pullback_low_resets_after_price_leaves_the_zone_above() -> None:
    """A stale low from an earlier, unrelated dip must not count toward a
    later pullback's reclaim once price has re-extended above the zone."""
    initial = market_quote(
        liquidity=50_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=15,
    )
    book = RecommendationBook(
        pool_size=10, ttl_seconds=60, entry_confirmation_polls=1
    )
    candidate = book.add(initial, CoinIntelligence().score(initial), now=0)
    assert candidate is not None

    def quote_at(price_multiplier: float, change_pct: float, volume: int = 16_000) -> MarketQuote:
        return MarketQuote(
            mint=initial.mint,
            symbol=initial.symbol,
            price_sol=initial.price_sol * price_multiplier,
            liquidity_usd=initial.liquidity_usd,
            market_cap_usd=initial.market_cap_usd,
            pair_address=initial.pair_address,
            pair_created_at_ms=initial.pair_created_at_ms,
            buys_m5=40,
            sells_m5=20,
            volume_m5_usd=volume,
            price_change_m5_pct=change_pct,
        )

    # Touches a very deep low inside the zone.
    book.update(quote_at(0.90, -5), now=3)
    assert candidate.pullback_low_price == pytest.approx(
        initial.price_sol * 0.90
    )

    # Price fully recovers back above the zone - the pullback episode ends.
    # Falling volume here keeps this above-zone step isolated from the
    # separate MOMENTUM BUY path (see test_momentum_buy_* in this file),
    # which would otherwise fire on this same strong, established move.
    book.update(quote_at(1.0, 5, volume=10_000), now=6)
    assert candidate.decision in {"WAIT FOR PULLBACK", "PULLBACK STARTED"}
    assert candidate.pullback_low_price is None

    # A fresh, shallow dip back into the zone should be judged on its own
    # low, not the earlier 0.90x trough from the unrelated prior dip - a
    # bounce off *this* low is still required before BUY ZONE fires.
    book.update(quote_at(0.94, 1), now=9)
    assert candidate.decision == "WATCH"
    assert candidate.pullback_low_price == pytest.approx(
        initial.price_sol * 0.94
    )
    book.update(quote_at(0.96, 1), now=12)
    assert candidate.decision == "BUY ZONE"


def test_pullback_started_is_detected_and_alerted_once() -> None:
    initial = market_quote(
        liquidity=50_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=15,
    )
    book = RecommendationBook(
        pool_size=10,
        ttl_seconds=60,
        pullback_started_pct=2,
    )
    candidate = book.add(initial, CoinIntelligence().score(initial), now=0)
    assert candidate is not None
    assert candidate.decision == "WAIT FOR PULLBACK"

    pullback = MarketQuote(
        mint=initial.mint,
        symbol=initial.symbol,
        price_sol=initial.price_sol * 0.97,
        liquidity_usd=initial.liquidity_usd,
        market_cap_usd=initial.market_cap_usd,
        pair_address=initial.pair_address,
        pair_created_at_ms=initial.pair_created_at_ms,
        buys_m5=35,
        sells_m5=20,
        volume_m5_usd=16_000,
        price_change_m5_pct=-1,
    )
    book.update(pullback, now=5)

    assert candidate.decision == "PULLBACK STARTED"
    assert candidate.pullback_from_peak_pct == pytest.approx(3)
    alerts = book.pop_pullback_alerts()
    assert alerts == [candidate]
    assert book.pop_pullback_alerts() == []

    output = format_dashboard(
        build_snapshot(
            book.ranked(),
            pending_count=0,
            poll_seconds=15,
            alerts=alerts,
        ),
        color=False,
    )
    assert "PULLBACK STARTED ALERT" in output
    assert "decision=PULLBACK STARTED" in output


def test_stale_entry_zone_re_anchors_to_a_new_peak() -> None:
    """A token that keeps making new highs must not stay permanently locked
    out of a confirmed-pullback entry by a zone anchored to its first, long
    since obsolete overextension.
    """
    initial = market_quote(
        liquidity=50_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=15,
    )
    book = RecommendationBook(
        pool_size=10, ttl_seconds=60, entry_confirmation_polls=1
    )
    candidate = book.add(initial, CoinIntelligence().score(initial), now=0)
    assert candidate is not None
    stale_zone_high = candidate.entry_zone_high

    def quote_at(price_multiplier: float, change_pct: float, volume: int = 16_000) -> MarketQuote:
        return MarketQuote(
            mint=initial.mint,
            symbol=initial.symbol,
            price_sol=initial.price_sol * price_multiplier,
            liquidity_usd=initial.liquidity_usd,
            market_cap_usd=initial.market_cap_usd,
            pair_address=initial.pair_address,
            pair_created_at_ms=initial.pair_created_at_ms,
            buys_m5=40,
            sells_m5=20,
            volume_m5_usd=volume,
            price_change_m5_pct=change_pct,
        )

    # Runs far past the old zone to a fresh new high - the stale zone must
    # not stay frozen 15x below current price. Falling volume keeps this
    # isolated from the separate MOMENTUM BUY path (see test_momentum_buy_*
    # in this file), which would otherwise fire on this same strong move.
    book.update(quote_at(15.0, 20, volume=10_000), now=3)
    assert candidate.decision == "WAIT FOR PULLBACK"
    assert candidate.entry_zone_high > stale_zone_high
    assert candidate.entry_zone_low == pytest.approx(
        initial.price_sol * 15.0 * 0.94
    )
    assert candidate.pullback_low_price is None

    # A genuine pullback into the *new* zone, followed by a reclaim off its
    # own tracked low, should still be able to fire BUY ZONE.
    book.update(quote_at(15.0 * 0.94, -3), now=6)
    assert candidate.decision == "WATCH"
    book.update(quote_at(15.0 * 0.96, 1), now=9)
    assert candidate.decision == "BUY ZONE"


def test_early_buy_fires_for_a_fresh_candidate_still_near_its_starting_price() -> None:
    """A brand-new candidate with real activity and no proven move yet
    qualifies through the deliberately weaker early-buy path, rather than
    waiting for either a pullback cycle or a momentum confirmation."""
    initial = market_quote(
        liquidity=50_000, market_cap=100_000, buys=60, sells=20,
        volume=15_000, change=0,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60, entry_confirmation_polls=1)
    candidate = book.add(initial, CoinIntelligence().score(initial), now=0)
    assert candidate is not None
    assert candidate.decision == "EARLY BUY"
    assert candidate.entry_zone_low is None
    assert "starting price" in candidate.decision_reason


def test_early_buy_does_not_fire_once_price_has_moved_well_off_its_start() -> None:
    """Once price has drifted well past its starting point, that's no
    longer 'still close to the start' - MOMENTUM BUY or the pullback path
    are the right tools for a token that has already moved."""
    initial = market_quote(
        liquidity=50_000, market_cap=100_000, buys=60, sells=20,
        volume=15_000, change=0,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60, entry_confirmation_polls=1)
    candidate = book.add(initial, CoinIntelligence().score(initial), now=0)
    assert candidate is not None
    assert candidate.decision == "EARLY BUY"

    dipped = MarketQuote(
        mint=initial.mint, symbol=initial.symbol,
        price_sol=initial.price_sol * 0.85,
        liquidity_usd=initial.liquidity_usd, market_cap_usd=initial.market_cap_usd,
        pair_address=initial.pair_address, pair_created_at_ms=initial.pair_created_at_ms,
        buys_m5=60, sells_m5=20, volume_m5_usd=15_000, price_change_m5_pct=-2,
    )
    book.update(dipped, now=3)
    assert candidate.decision != "EARLY BUY"


def test_early_buy_requires_its_own_higher_liquidity_floor() -> None:
    """A brand-new token has no track record at all, so early-buy holds it
    to a higher liquidity bar than the general AVOID floor - thin enough to
    clear that floor but not this one should not qualify."""
    initial = market_quote(
        liquidity=8_000, market_cap=100_000, buys=60, sells=20,
        volume=15_000, change=0,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60, entry_confirmation_polls=1)
    candidate = book.add(initial, CoinIntelligence().score(initial), now=0)
    assert candidate is not None
    assert candidate.decision != "EARLY BUY"


def test_early_buy_does_not_fire_on_a_token_actively_falling() -> None:
    """Still being within the starting-price window is not itself a reason
    to buy - a token actively declining in the last five minutes (here,
    -3% - FALLING territory) must not qualify just because it hasn't
    fallen far enough yet to leave the window."""
    initial = market_quote(
        liquidity=50_000, market_cap=100_000, buys=60, sells=20,
        volume=15_000, change=-3,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60, entry_confirmation_polls=1)
    candidate = book.add(initial, CoinIntelligence().score(initial), now=0)
    assert candidate is not None
    assert candidate.momentum_label == "FALLING"
    assert candidate.decision != "EARLY BUY"


def test_early_buy_still_fires_on_flat_or_unknown_momentum() -> None:
    """A mild dip within STABLE range, or no five-minute data yet at all
    (the genuinely earliest case), is not 'falling' and should still
    qualify - only active decline is excluded."""
    initial = market_quote(
        liquidity=50_000, market_cap=100_000, buys=60, sells=20,
        volume=15_000, change=-1,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60, entry_confirmation_polls=1)
    candidate = book.add(initial, CoinIntelligence().score(initial), now=0)
    assert candidate is not None
    assert candidate.momentum_label == "STABLE"
    assert candidate.decision == "EARLY BUY"


def test_momentum_buy_confirms_on_the_first_poll_even_with_the_general_threshold_at_three() -> None:
    """MOMENTUM BUY uses its own, lower confirmation threshold
    (momentum_buy_confirmation_polls, default 1) independent of the general
    entry_confirmation_polls (default 3) used by the pullback/BUY NOW
    paths - requiring the same strong-momentum reading to persist for 3
    consecutive polls (~30-45s) tends to select for local tops, since a
    move that's confirmed strong three times running is often already
    exhausting (observed live: BETBOLT, WETCAT, and WHT all bought within
    seconds of a local peak this way, then reversed hard)."""
    initial = market_quote(
        liquidity=50_000, market_cap=100_000, buys=60, sells=20,
        volume=15_000, change=1,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60)
    assert book.entry_confirmation_polls == 3
    assert book.momentum_buy_confirmation_polls == 1
    candidate = book.add(initial, CoinIntelligence().score(initial), now=0)
    assert candidate is not None

    strong_move = MarketQuote(
        mint=initial.mint, symbol=initial.symbol,
        price_sol=initial.price_sol * 1.10,
        liquidity_usd=initial.liquidity_usd, market_cap_usd=initial.market_cap_usd,
        pair_address=initial.pair_address, pair_created_at_ms=initial.pair_created_at_ms,
        buys_m5=45, sells_m5=15, volume_m5_usd=20_000, price_change_m5_pct=6,
    )
    book.update(strong_move, now=3)

    assert candidate.decision == "MOMENTUM BUY"
    assert candidate.entry_confirmation_count == 1
    assert candidate.entry_confirmation_required == 1


def test_momentum_buy_fires_on_a_fresh_extended_move_with_rising_volume() -> None:
    """A token that extends straight up without ever giving back into a
    pullback zone must not be permanently unbuyable just because the
    pullback-only path requires a dip that may never come - given genuinely
    strong, independently-confirmed evidence (rising volume, firm buy
    pressure), it qualifies through a separate, stricter momentum path.
    """
    initial = market_quote(
        liquidity=50_000, market_cap=100_000, buys=60, sells=20,
        volume=15_000, change=1,
    )
    book = RecommendationBook(
        pool_size=10, ttl_seconds=60, entry_confirmation_polls=1,
        momentum_buy_min_ratio=1.5,
    )
    candidate = book.add(initial, CoinIntelligence().score(initial), now=0)
    assert candidate is not None
    assert candidate.entry_zone_low is None

    strong_move = MarketQuote(
        mint=initial.mint,
        symbol=initial.symbol,
        price_sol=initial.price_sol * 1.10,
        liquidity_usd=initial.liquidity_usd,
        market_cap_usd=initial.market_cap_usd,
        pair_address=initial.pair_address,
        pair_created_at_ms=initial.pair_created_at_ms,
        buys_m5=45,
        sells_m5=15,
        volume_m5_usd=20_000,
        price_change_m5_pct=6,
    )
    book.update(strong_move, now=3)
    assert candidate.decision == "MOMENTUM BUY"
    assert candidate.entry_zone_low is None
    assert "momentum continuation" in candidate.decision_reason


def test_momentum_buy_does_not_fire_on_a_fading_pump() -> None:
    """The same extended move without rising volume - a token grinding
    higher on fading interest - must not qualify: this is exactly the
    "buying a top" scenario the strict volume bar exists to exclude."""
    initial = market_quote(
        liquidity=50_000, market_cap=100_000, buys=60, sells=20,
        volume=15_000, change=1,
    )
    book = RecommendationBook(
        pool_size=10, ttl_seconds=60, entry_confirmation_polls=1,
        momentum_buy_min_ratio=1.5,
    )
    candidate = book.add(initial, CoinIntelligence().score(initial), now=0)
    assert candidate is not None

    fading_move = MarketQuote(
        mint=initial.mint,
        symbol=initial.symbol,
        price_sol=initial.price_sol * 1.10,
        liquidity_usd=initial.liquidity_usd,
        market_cap_usd=initial.market_cap_usd,
        pair_address=initial.pair_address,
        pair_created_at_ms=initial.pair_created_at_ms,
        buys_m5=45,
        sells_m5=15,
        volume_m5_usd=10_000,  # falling from the 15,000 baseline
        price_change_m5_pct=6,
    )
    book.update(fading_move, now=3)
    assert candidate.decision != "MOMENTUM BUY"
    assert candidate.entry_zone_low is not None


def test_momentum_buy_does_not_fire_on_a_thin_trade_sample() -> None:
    """A 3:1 buy/sell ratio from only a handful of trades isn't real evidence
    of firm buy pressure - the count floor exists so a thin, easily-skewed
    sample can't pass as confirmation the way COPPERCAT/XtraPad's thousands
    of real trades did."""
    initial = market_quote(
        liquidity=50_000, market_cap=100_000, buys=60, sells=20,
        volume=15_000, change=1,
    )
    book = RecommendationBook(
        pool_size=10, ttl_seconds=60, entry_confirmation_polls=1,
        momentum_buy_min_ratio=1.5, momentum_buy_min_trades=50,
    )
    candidate = book.add(initial, CoinIntelligence().score(initial), now=0)
    assert candidate is not None

    thin_sample = MarketQuote(
        mint=initial.mint,
        symbol=initial.symbol,
        price_sol=initial.price_sol * 1.10,
        liquidity_usd=initial.liquidity_usd,
        market_cap_usd=initial.market_cap_usd,
        pair_address=initial.pair_address,
        pair_created_at_ms=initial.pair_created_at_ms,
        buys_m5=9,
        sells_m5=3,  # ratio 3.0, well above the bar - but only 12 total trades
        volume_m5_usd=20_000,
        price_change_m5_pct=6,
    )
    book.update(thin_sample, now=3)
    assert candidate.decision != "MOMENTUM BUY"


def test_momentum_buy_fires_via_liquidity_growth_despite_a_low_ratio() -> None:
    """A near-even transaction count (ratio below the bar) can still reflect
    a genuine pump if real new capital is flowing in - liquidity growth is
    direct evidence a count ratio alone can't see (matches the real-world
    '$' token: 32.6% liquidity growth alongside only a 1.093 ratio)."""
    initial = market_quote(
        liquidity=50_000, market_cap=100_000, buys=60, sells=20,
        volume=15_000, change=1,
    )
    book = RecommendationBook(
        pool_size=10, ttl_seconds=60, entry_confirmation_polls=1,
        momentum_buy_min_ratio=1.5, momentum_buy_min_liquidity_growth_pct=20.0,
    )
    candidate = book.add(initial, CoinIntelligence().score(initial), now=0)
    assert candidate is not None

    low_ratio_but_liquid = MarketQuote(
        mint=initial.mint,
        symbol=initial.symbol,
        price_sol=initial.price_sol * 1.10,
        liquidity_usd=65_000,  # 30% growth from the 50,000 baseline
        market_cap_usd=initial.market_cap_usd,
        pair_address=initial.pair_address,
        pair_created_at_ms=initial.pair_created_at_ms,
        buys_m5=110,
        sells_m5=100,  # ratio 1.10, below the 1.5 bar
        volume_m5_usd=20_000,
        price_change_m5_pct=6,
    )
    book.update(low_ratio_but_liquid, now=3)
    assert candidate.decision == "MOMENTUM BUY"
    assert "liquidity up" in candidate.decision_reason


def test_momentum_buy_does_not_fire_without_ratio_or_liquidity_confirmation() -> None:
    """Neither signal clears its bar - no confirmation, no entry."""
    initial = market_quote(
        liquidity=50_000, market_cap=100_000, buys=60, sells=20,
        volume=15_000, change=1,
    )
    book = RecommendationBook(
        pool_size=10, ttl_seconds=60, entry_confirmation_polls=1,
        momentum_buy_min_ratio=1.5, momentum_buy_min_liquidity_growth_pct=20.0,
    )
    candidate = book.add(initial, CoinIntelligence().score(initial), now=0)
    assert candidate is not None

    weak_move = MarketQuote(
        mint=initial.mint,
        symbol=initial.symbol,
        price_sol=initial.price_sol * 1.10,
        liquidity_usd=52_000,  # only 4% growth
        market_cap_usd=initial.market_cap_usd,
        pair_address=initial.pair_address,
        pair_created_at_ms=initial.pair_created_at_ms,
        buys_m5=110,
        sells_m5=100,  # ratio 1.10, below the 1.5 bar
        volume_m5_usd=20_000,
        price_change_m5_pct=6,
    )
    book.update(weak_move, now=3)
    assert candidate.decision != "MOMENTUM BUY"


def test_momentum_buy_fires_above_an_already_anchored_zone_without_re_anchoring() -> None:
    """A token that already has an anchored zone from an earlier
    overextension, and is now grinding above it without yet running far
    enough to re-anchor (see test_stale_entry_zone_re_anchors_to_a_new_peak),
    should still qualify via the momentum path given strong enough evidence -
    this is the case a purely re-anchor-based fix could never reach."""
    initial = market_quote(
        liquidity=50_000, market_cap=100_000, buys=60, sells=20,
        volume=15_000, change=15,
    )
    book = RecommendationBook(
        pool_size=10, ttl_seconds=60, entry_confirmation_polls=1,
        momentum_buy_min_ratio=1.5,
    )
    candidate = book.add(initial, CoinIntelligence().score(initial), now=0)
    assert candidate is not None
    zone_high = candidate.entry_zone_high
    assert zone_high is not None

    just_above_zone = MarketQuote(
        mint=initial.mint,
        symbol=initial.symbol,
        price_sol=zone_high * 1.02,  # above the zone, well under the 8% re-anchor trigger
        liquidity_usd=initial.liquidity_usd,
        market_cap_usd=initial.market_cap_usd,
        pair_address=initial.pair_address,
        pair_created_at_ms=initial.pair_created_at_ms,
        buys_m5=45,
        sells_m5=15,
        volume_m5_usd=20_000,
        price_change_m5_pct=6,
    )
    book.update(just_above_zone, now=3)
    assert candidate.decision == "MOMENTUM BUY"


def test_momentum_buy_accepts_steady_volume_once_a_zone_is_already_anchored() -> None:
    """Unlike a candidate's very first overextended observation (where
    "steady" volume is trivially true - initial_volume_m5_usd is bootstrapped
    from that same first quote), a candidate that has already survived one
    full overextend-and-anchor cycle has real accumulated history behind a
    steady reading, so it's accepted here even without volume accelerating
    further - this is the exact case that motivated loosening the bar
    (see test_momentum_buy_fires_on_a_fresh_extended_move_with_rising_volume
    for why a first-observation candidate still requires RISING)."""
    initial = market_quote(
        liquidity=50_000, market_cap=100_000, buys=60, sells=20,
        volume=15_000, change=15,
    )
    book = RecommendationBook(
        pool_size=10, ttl_seconds=60, entry_confirmation_polls=1,
        momentum_buy_min_ratio=1.5,
    )
    candidate = book.add(initial, CoinIntelligence().score(initial), now=0)
    assert candidate is not None
    zone_high = candidate.entry_zone_high
    assert zone_high is not None

    just_above_zone = MarketQuote(
        mint=initial.mint,
        symbol=initial.symbol,
        price_sol=zone_high * 1.02,
        liquidity_usd=initial.liquidity_usd,
        market_cap_usd=initial.market_cap_usd,
        pair_address=initial.pair_address,
        pair_created_at_ms=initial.pair_created_at_ms,
        buys_m5=45,
        sells_m5=15,
        volume_m5_usd=15_500,  # 15,500 / 15,000 = 1.033 -> STEADY, not RISING
        price_change_m5_pct=6,
    )
    book.update(just_above_zone, now=3)
    assert candidate.decision == "MOMENTUM BUY"


def test_entry_requires_three_consecutive_confirmations() -> None:
    quote = market_quote(
        liquidity=50_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=5,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60)
    candidate = book.add(quote, CoinIntelligence().score(quote), now=0)

    assert candidate is not None
    assert candidate.decision == "ENTRY PENDING"
    assert candidate.entry_confirmation_count == 1

    book.update(quote, now=5)
    assert candidate.decision == "ENTRY PENDING"
    assert candidate.entry_confirmation_count == 2

    book.update(quote, now=10)
    assert candidate.decision == "EARLY BUY"
    assert candidate.entry_confirmation_count == 3
    assert "confirmed for 3 consecutive checks" in candidate.decision_reason
    assert candidate.planned_entry_price == pytest.approx(quote.price_sol)
    assert candidate.planned_stop_price == pytest.approx(quote.price_sol * 0.8)
    assert candidate.planned_target_price == pytest.approx(quote.price_sol * 1.4)
    assert candidate.planned_reward_risk_ratio == pytest.approx(2)


def test_auto_buy_discovery_requires_confirmed_fresh_liquid_signal(
    tmp_path: Path,
) -> None:
    quote = market_quote(
        liquidity=60_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=5,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60)
    candidate = book.add(quote, CoinIntelligence().score(quote), now=0)
    assert candidate is not None
    book.update(quote, now=5)
    book.update(quote, now=10)
    assert candidate.decision == "EARLY BUY"

    config = settings(
        tmp_path / "discovery.db",
        auto_buy_enabled=True,
        auto_buy_discovery=True,
        auto_buy_discovery_min_score=70,
        auto_buy_discovery_min_liquidity_usd=50_000,
        auto_buy_signal_max_age_seconds=30,
    )
    assert auto_buy_discovery_rejection(
        candidate, config, now=20
    ) is None
    assert auto_buy_discovery_rejection(
        candidate, config, now=41
    ) == "signal is stale"

    candidate.liquidity_usd = 49_999
    assert auto_buy_discovery_rejection(
        candidate, config, now=20
    ) == "liquidity is below the automatic-discovery minimum"


def test_auto_buy_discovery_arms_a_new_qualified_mint(
    tmp_path: Path,
) -> None:
    quote = market_quote(
        liquidity=60_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=5,
    )
    now = 1_000.0
    book = RecommendationBook(pool_size=10, ttl_seconds=60)
    candidate = book.add(quote, CoinIntelligence().score(quote), now=now)
    assert candidate is not None
    book.update(quote, now=now + 5)
    book.update(quote, now=now + 10)
    candidate.updated_at = time.time()
    assert candidate.decision == "EARLY BUY"

    store = SQLiteStore(str(tmp_path / "discovery-arm.db"))
    config = settings(
        tmp_path / "discovery-arm.db",
        auto_buy_enabled=True,
        auto_buy_discovery=True,
        auto_buy_discovery_min_score=70,
        auto_buy_discovery_min_liquidity_usd=50_000,
        auto_buy_signal_max_age_seconds=30,
    )
    guard = LaunchGuard(config, store)
    asyncio.run(guard._maybe_auto_buy(candidate))

    policy = store.load_auto_buy_policy(candidate.mint)
    assert policy is not None
    assert policy["armed"] == 1
    assert policy["symbol"] == candidate.symbol
    store.close()


def test_falling_volume_blocks_entry_confirmation() -> None:
    initial = market_quote(
        liquidity=50_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=15,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60)
    candidate = book.add(initial, CoinIntelligence().score(initial), now=0)
    assert candidate is not None
    assert candidate.decision == "WAIT FOR PULLBACK"

    weak_pullback = MarketQuote(
        mint=initial.mint,
        symbol=initial.symbol,
        price_sol=initial.price_sol * 0.95,
        liquidity_usd=initial.liquidity_usd,
        market_cap_usd=initial.market_cap_usd,
        pair_address=initial.pair_address,
        pair_created_at_ms=initial.pair_created_at_ms,
        buys_m5=40,
        sells_m5=20,
        volume_m5_usd=5_000,
        price_change_m5_pct=2,
    )
    book.update(weak_pullback, now=5)

    assert candidate.volume_label == "FALLING"
    assert candidate.decision == "WATCH"
    assert candidate.decision_reason == "entry blocked: five-minute volume is falling"
    assert candidate.entry_confirmation_count == 0


def test_entry_decision_avoids_heavy_selloff() -> None:
    initial = market_quote(
        liquidity=50_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=5,
    )
    book = RecommendationBook(
        pool_size=10, ttl_seconds=60, entry_confirmation_polls=1
    )
    candidate = book.add(initial, CoinIntelligence().score(initial), now=0)
    assert candidate is not None
    assert candidate.decision == "EARLY BUY"

    selloff = MarketQuote(
        mint=initial.mint,
        symbol=initial.symbol,
        price_sol=initial.price_sol * 0.9,
        liquidity_usd=initial.liquidity_usd,
        market_cap_usd=initial.market_cap_usd,
        pair_address=initial.pair_address,
        pair_created_at_ms=initial.pair_created_at_ms,
        buys_m5=10,
        sells_m5=30,
        volume_m5_usd=20_000,
        price_change_m5_pct=-10,
    )
    book.update(selloff, now=5)

    assert candidate.decision == "AVOID"
    assert candidate.decision_reason == "falling price with heavy selling"


def test_owned_portfolio_exit_can_trigger_without_cost_basis(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(str(tmp_path / "owned-exit.db"))
    config = settings(
        tmp_path / "owned-exit.db",
        auto_sell_enabled=True,
        auto_sell_live=False,
        auto_sell_portfolio_signals=True,
        auto_sell_min_value_usd=1.0,
        auto_sell_signal_confirmation_polls=1,
    )
    guard = LaunchGuard(config, store)
    signal = PortfolioSignal(
        chain="solana",
        token_address="MintOwned111",
        symbol="OWN",
        quantity=100,
        current_price=0.1,
        price_currency="USD",
        current_value_usd=10.0,
        pnl_pct=None,
        decision="EXIT WARNING",
        reason="momentum reversal",
        raw_decision="EXIT WARNING",
        price_change_m5_pct=-10,
        buys_m5=2,
        sells_m5=8,
        liquidity_usd=20_000,
        entry_price=None,
        peak_price=0.12,
    )
    balance = SolanaTokenHolding(
        mint="MintOwned111",
        amount=100,
        raw_amount=100_000_000,
        decimals=6,
    )

    asyncio.run(guard._maybe_auto_sell(signal, balance))

    assert (
        "solana:MintOwned111:portfolio-signal:exit-warning"
        in guard.auto_sell_dry_run_seen
    )
    store.close()


def test_live_portfolio_exit_executes_one_persistent_chunk_per_poll(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(str(tmp_path / "owned-live-chunks.db"))
    config = settings(
        tmp_path / "owned-live-chunks.db",
        auto_sell_enabled=True,
        auto_sell_live=False,
        auto_sell_portfolio_signals=True,
        auto_sell_adaptive_chunks=True,
        auto_sell_min_chunk_fraction=0.01,
        auto_sell_max_chunk_attempts=8,
        auto_sell_signal_confirmation_polls=1,
    )
    guard = LaunchGuard(config, store)
    prepared_amounts: list[int] = []

    class FakeSeller:
        async def preflight_adaptive(
            self,
            intent: SellIntent,
            _simulator: object,
            *,
            minimum_amount_raw: int,
            max_attempts: int,
        ) -> PreflightReceipt:
            assert minimum_amount_raw > 0
            assert max_attempts == 8
            selected_raw = (
                40_000_000
                if intent.amount_raw > 60_000_000
                else intent.amount_raw
            )
            selected_intent = replace(intent, amount_raw=selected_raw)
            prepared_amounts.append(selected_raw)
            return PreflightReceipt(
                prepared=PreparedSell(
                    intent=selected_intent,
                    transaction="unsigned",
                    request_id=f"request-{len(prepared_amounts)}",
                    input_amount_raw=selected_raw,
                    expected_output_raw=selected_raw // 10,
                    minimum_output_raw=selected_raw // 11,
                    price_impact_pct=1.0,
                    last_valid_block_height="123",
                    slippage_bps=500,
                ),
                units_consumed=200_000,
                log_count=10,
                adaptive_attempts=2 if len(prepared_amounts) == 1 else 1,
            )

        async def execute(self, prepared: PreparedSell) -> SellReceipt:
            return SellReceipt(
                intent=prepared.intent,
                signature=f"sig-{len(prepared_amounts)}",
                input_amount_raw=prepared.input_amount_raw,
                output_amount_raw=prepared.expected_output_raw,
            )

    guard.auto_seller = FakeSeller()  # type: ignore[assignment]
    signal = PortfolioSignal(
        chain="solana",
        token_address="MintOwned111",
        symbol="OWN",
        quantity=100,
        current_price=0.1,
        price_currency="USD",
        current_value_usd=10.0,
        pnl_pct=None,
        decision="EXIT WARNING",
        reason="momentum reversal",
        raw_decision="EXIT WARNING",
        price_change_m5_pct=-10,
        buys_m5=2,
        sells_m5=8,
        liquidity_usd=20_000,
        entry_price=None,
        peak_price=0.12,
    )

    asyncio.run(
        guard._maybe_auto_sell(
            signal,
            SolanaTokenHolding(
                mint="MintOwned111",
                amount=100,
                raw_amount=100_000_000,
                decimals=6,
            ),
        )
    )
    batch_values = {
        "batch_key": "solana:MintOwned111:portfolio-signal:exit-warning",
        "chain": "solana",
        "token_address": "MintOwned111",
        "symbol": "OWN",
        "stage": 12,
        "target_raw": 100_000_000,
        "full_exit": True,
    }
    batch = store.load_or_create_auto_sell_batch(**batch_values)
    assert batch["status"] == "ACTIVE"
    assert batch["sold_raw"] == 40_000_000
    assert batch["next_chunk_index"] == 1

    asyncio.run(
        guard._maybe_auto_sell(
            signal,
            SolanaTokenHolding(
                mint="MintOwned111",
                amount=60,
                raw_amount=60_000_000,
                decimals=6,
            ),
        )
    )
    batch = store.load_or_create_auto_sell_batch(**batch_values)
    assert prepared_amounts == [40_000_000, 60_000_000]
    assert batch["status"] == "CONFIRMED"
    assert batch["sold_raw"] == 100_000_000
    assert batch["next_chunk_index"] == 2
    store.close()


def test_portfolio_sell_signal_requires_confirmation_across_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sell-confirmation.db"
    store = SQLiteStore(str(path))
    config = settings(
        path,
        auto_sell_enabled=True,
        auto_sell_portfolio_signals=True,
        auto_sell_signal_confirmation_polls=3,
    )
    signal = PortfolioSignal(
        chain="solana",
        token_address="MintConfirm111",
        symbol="CONFIRM",
        quantity=100,
        current_price=0.1,
        price_currency="USD",
        current_value_usd=10,
        pnl_pct=None,
        decision="EXIT WARNING",
        reason="momentum reversal",
        raw_decision="EXIT WARNING",
        price_change_m5_pct=-10,
        buys_m5=2,
        sells_m5=8,
        liquidity_usd=20_000,
        entry_price=None,
        peak_price=0.12,
    )
    balance = SolanaTokenHolding(
        mint=signal.token_address,
        amount=100,
        raw_amount=100_000_000,
        decimals=6,
    )
    guard = LaunchGuard(config, store)
    asyncio.run(guard._maybe_auto_sell(signal, balance))
    asyncio.run(guard._maybe_auto_sell(signal, balance))
    assert not guard.auto_sell_dry_run_seen
    store.close()

    restarted_store = SQLiteStore(str(path))
    restarted = LaunchGuard(config, restarted_store)
    asyncio.run(restarted._maybe_auto_sell(signal, balance))
    assert (
        "solana:MintConfirm111:portfolio-signal:exit-warning"
        in restarted.auto_sell_dry_run_seen
    )
    restarted_store.close()


def test_portfolio_sell_confirmation_resets_on_hold_and_stale_gap(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(str(tmp_path / "sell-reset.db"))
    first = store.record_auto_sell_signal_confirmation(
        token_address="MintConfirm111",
        decision="EXIT WARNING",
        reason="selloff",
        observed_at_epoch=100,
        max_gap_seconds=30,
    )
    second = store.record_auto_sell_signal_confirmation(
        token_address="MintConfirm111",
        decision="EXIT WARNING",
        reason="selloff",
        observed_at_epoch=110,
        max_gap_seconds=30,
    )
    stale = store.record_auto_sell_signal_confirmation(
        token_address="MintConfirm111",
        decision="EXIT WARNING",
        reason="selloff",
        observed_at_epoch=200,
        max_gap_seconds=30,
    )
    assert first["consecutive_polls"] == 1
    assert second["consecutive_polls"] == 2
    assert stale["consecutive_polls"] == 1
    store.clear_auto_sell_signal_confirmation("MintConfirm111")
    reset = store.record_auto_sell_signal_confirmation(
        token_address="MintConfirm111",
        decision="EXIT WARNING",
        reason="selloff",
        observed_at_epoch=210,
        max_gap_seconds=30,
    )
    assert reset["consecutive_polls"] == 1
    store.close()


def test_auto_rebuy_requires_drop_rebound_momentum_and_liquidity(
    tmp_path: Path,
) -> None:
    config = settings(
        tmp_path / "rebuy-assessment.db",
        auto_buy_enabled=True,
        auto_sell_enabled=True,
        auto_rebuy_enabled=True,
    )
    now = time.time()
    watch = {
        "sold_at_epoch": now - 700,
        "exit_price_usd": 1.0,
        "exit_liquidity_usd": 100_000,
        "lowest_price_usd": 0.8,
        "last_price_usd": 0.82,
    }
    recovery = MarketQuote(
        mint="MintRecovery111",
        symbol="RECOVER",
        price_sol=0.001,
        price_usd=0.85,
        liquidity_usd=90_000,
        market_cap_usd=200_000,
        pair_address="PairRecovery",
        pair_created_at_ms=1,
        buys_m5=14,
        sells_m5=5,
        volume_m5_usd=25_000,
        price_change_m5_pct=4.0,
    )

    accepted, reason, metrics = auto_rebuy_recovery_assessment(
        watch, recovery, config, now=now
    )
    assert accepted is True
    assert "recovery confirmed" in reason
    assert metrics["drop_pct"] == pytest.approx(20)
    assert metrics["rebound_pct"] == pytest.approx(6.25)

    falling = replace(recovery, price_usd=0.79, price_change_m5_pct=-2)
    accepted, reason, _metrics = auto_rebuy_recovery_assessment(
        watch, falling, config, now=now
    )
    assert accepted is False
    assert "momentum" in reason
    assert "not rising" in reason


def test_auto_rebuy_watch_lifecycle_creates_new_sell_cycle(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(str(tmp_path / "rebuy-watch.db"))
    watch = store.start_auto_rebuy_watch(
        token_address="MintRecovery111",
        symbol="RECOVER",
        sell_signature="sell-1",
        exit_price_usd=1.0,
        exit_liquidity_usd=100_000,
        sale_proceeds_usdc_raw=10_000_000,
        sold_at_epoch=100,
        max_rebuys=2,
    )
    assert watch is not None
    assert watch["cycle"] == 1
    for poll in range(3):
        watch = store.record_auto_rebuy_observation(
            token_address="MintRecovery111",
            current_price_usd=0.8 + poll * 0.01,
            observed_at_epoch=200 + poll,
            qualifying=True,
            confirmation_required=3,
            reason="recovery confirmed",
        )
    assert watch["status"] == "READY"
    completed = store.complete_auto_rebuy(
        token_address="MintRecovery111", buy_signature="buy-1"
    )
    assert completed["status"] == "BOUGHT"
    assert completed["completed_rebuys"] == 1
    assert store.auto_sell_cycle("MintRecovery111") == 1

    planner = PortfolioSignalExitPlanner()
    sell = planner.plan(
        mint="MintRecovery111",
        symbol="RECOVER",
        decision="EXIT WARNING",
        reason="new decline",
        balance_raw=100,
        decimals=6,
        cycle=store.auto_sell_cycle("MintRecovery111"),
    )
    assert sell is not None
    assert sell.event_key.endswith(":cycle:1")
    assert store.start_auto_rebuy_watch(
        token_address="MintRecovery111",
        symbol="RECOVER",
        sell_signature="sell-2",
        exit_price_usd=0.9,
        exit_liquidity_usd=90_000,
        sale_proceeds_usdc_raw=9_000_000,
        sold_at_epoch=300,
        max_rebuys=1,
    ) is None
    second_watch = store.start_auto_rebuy_watch(
        token_address="MintRecovery111",
        symbol="RECOVER",
        sell_signature="sell-2",
        exit_price_usd=0.9,
        exit_liquidity_usd=90_000,
        sale_proceeds_usdc_raw=9_000_000,
        sold_at_epoch=300,
        max_rebuys=2,
    )
    assert second_watch is not None
    assert second_watch["cycle"] == 2
    assert store.cancel_auto_rebuy("MintRecovery111") is True
    store.close()


def test_uncertain_rebuy_is_frozen_without_automatic_retry(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(str(tmp_path / "rebuy-review.db"))
    watch = store.start_auto_rebuy_watch(
        token_address="MintRecovery111",
        symbol="RECOVER",
        sell_signature="sell-1",
        exit_price_usd=1.0,
        exit_liquidity_usd=100_000,
        sale_proceeds_usdc_raw=10_000_000,
        sold_at_epoch=100,
        max_rebuys=1,
    )
    assert watch is not None
    event_key = "solana:MintRecovery111:auto-rebuy:1"
    assert store.begin_auto_buy_execution(
        event_key=event_key,
        token_address="MintRecovery111",
        symbol="RECOVER",
        funding_source="seed",
        input_usdc_raw=5_000_000,
        expected_output_raw=6_000_000,
    )
    store.freeze_auto_buy_execution(
        event_key=event_key,
        error="execution outcome unknown",
        signature="public-signature",
    )
    store.freeze_auto_rebuy(
        token_address="MintRecovery111",
        error="execution outcome unknown; signature=public-signature",
    )

    assert store.active_auto_rebuy_watches() == []
    status = store.auto_rebuy_status()
    assert status["watches"][0]["status"] == "REVIEW"
    assert status["executions"][0]["status"] == "REVIEW"
    assert status["executions"][0]["signature"] == "public-signature"
    assert store.begin_auto_buy_execution(
        event_key=event_key,
        token_address="MintRecovery111",
        symbol="RECOVER",
        funding_source="seed",
        input_usdc_raw=5_000_000,
        expected_output_raw=6_000_000,
    ) is False
    store.close()


def test_confirmed_full_exit_starts_rebuy_watch(tmp_path: Path) -> None:
    path = tmp_path / "rebuy-after-sale.db"
    store = SQLiteStore(str(path))
    config = settings(
        path,
        auto_sell_enabled=True,
        auto_sell_portfolio_signals=True,
        auto_sell_signal_confirmation_polls=1,
        auto_buy_enabled=True,
        auto_rebuy_enabled=True,
    )
    guard = LaunchGuard(config, store)

    class FakeSeller:
        async def preflight(
            self, intent: SellIntent, _simulator: object
        ) -> PreflightReceipt:
            return PreflightReceipt(
                prepared=PreparedSell(
                    intent=intent,
                    transaction="unsigned",
                    request_id="request-sell",
                    input_amount_raw=intent.amount_raw,
                    expected_output_raw=8_000_000,
                    minimum_output_raw=7_900_000,
                    price_impact_pct=1.0,
                    last_valid_block_height="123",
                ),
                units_consumed=100_000,
                log_count=5,
            )

        async def execute(self, prepared: PreparedSell) -> SellReceipt:
            return SellReceipt(
                intent=prepared.intent,
                signature="sell-confirmed",
                input_amount_raw=prepared.input_amount_raw,
                output_amount_raw=8_000_000,
            )

    guard.auto_seller = FakeSeller()  # type: ignore[assignment]
    signal = PortfolioSignal(
        chain="solana",
        token_address="MintRecovery111",
        symbol="RECOVER",
        quantity=100,
        current_price=0.1,
        price_currency="USD",
        current_value_usd=10,
        pnl_pct=None,
        decision="EXIT WARNING",
        reason="confirmed selloff",
        raw_decision="EXIT WARNING",
        price_change_m5_pct=-10,
        buys_m5=2,
        sells_m5=8,
        liquidity_usd=50_000,
        entry_price=None,
        peak_price=0.12,
    )
    balance = SolanaTokenHolding(
        mint=signal.token_address,
        amount=100,
        raw_amount=100_000_000,
        decimals=6,
    )

    asyncio.run(guard._maybe_auto_sell(signal, balance))

    watch = store.load_auto_rebuy_watch(signal.token_address)
    assert watch is not None
    assert watch["status"] == "WATCHING"
    assert watch["sell_signature"] == "sell-confirmed"
    assert watch["exit_price_usd"] == pytest.approx(0.08)
    assert watch["sale_proceeds_usdc_raw"] == 8_000_000
    store.close()


def test_confirmed_protect_profit_exit_also_starts_rebuy_watch(tmp_path: Path) -> None:
    """A considerable rise that reverses and gets fully sold to protect
    profit deserves a rebuy watch exactly as much as a loss-driven exit -
    it is not a mistake to walk away from, it is a real re-entry
    opportunity once price is low again.
    """
    path = tmp_path / "rebuy-after-profit-sale.db"
    store = SQLiteStore(str(path))
    config = settings(
        path,
        auto_sell_enabled=True,
        auto_sell_portfolio_signals=True,
        auto_sell_signal_confirmation_polls=1,
        auto_buy_enabled=True,
        auto_rebuy_enabled=True,
    )
    guard = LaunchGuard(config, store)
    # PROTECT PROFIT (unlike EXIT WARNING) requires a verified USD cost
    # basis before it will execute at all.
    store.save_owned_holding(
        OwnedHolding(
            chain="solana",
            token_address="MintProfit111",
            symbol="PROFIT",
            quantity=100,
            entry_price=0.1,
            price_currency="USD",
            cost_amount=10,
        )
    )

    class FakeSeller:
        async def preflight(
            self, intent: SellIntent, _simulator: object
        ) -> PreflightReceipt:
            return PreflightReceipt(
                prepared=PreparedSell(
                    intent=intent,
                    transaction="unsigned",
                    request_id="request-sell",
                    input_amount_raw=intent.amount_raw,
                    expected_output_raw=24_000_000,
                    minimum_output_raw=23_800_000,
                    price_impact_pct=1.0,
                    last_valid_block_height="123",
                ),
                units_consumed=100_000,
                log_count=5,
            )

        async def execute(self, prepared: PreparedSell) -> SellReceipt:
            return SellReceipt(
                intent=prepared.intent,
                signature="protect-profit-sell-confirmed",
                input_amount_raw=prepared.input_amount_raw,
                output_amount_raw=24_000_000,
            )

    guard.auto_seller = FakeSeller()  # type: ignore[assignment]
    signal = PortfolioSignal(
        chain="solana",
        token_address="MintProfit111",
        symbol="PROFIT",
        quantity=100,
        current_price=0.24,
        price_currency="USD",
        current_value_usd=24,
        pnl_pct=140,
        decision="PROTECT PROFIT",
        reason="price is 15.0% below its monitored peak; open gain is 140.0%",
        raw_decision="PROTECT PROFIT",
        price_change_m5_pct=-4,
        buys_m5=5,
        sells_m5=6,
        liquidity_usd=50_000,
        entry_price=0.1,
        peak_price=0.28,
    )
    balance = SolanaTokenHolding(
        mint=signal.token_address,
        amount=100,
        raw_amount=100_000_000,
        decimals=6,
    )

    asyncio.run(guard._maybe_auto_sell(signal, balance))

    watch = store.load_auto_rebuy_watch(signal.token_address)
    assert watch is not None
    assert watch["status"] == "WATCHING"
    assert watch["sell_signature"] == "protect-profit-sell-confirmed"
    assert watch["exit_price_usd"] == pytest.approx(0.24)
    assert watch["sale_proceeds_usdc_raw"] == 24_000_000
    store.close()


def test_rebuy_resets_profit_ladder_stage(tmp_path: Path) -> None:
    store = SQLiteStore(str(tmp_path / "rebuy-stage.db"))
    store.save_owned_holding(
        OwnedHolding(
            chain="solana",
            token_address="MintRecovery111",
            symbol="RECOVER",
            quantity=100,
            entry_price=1,
            price_currency="USD",
            cost_amount=100,
        )
    )
    store.arm_auto_sell("MintRecovery111")
    assert store.begin_auto_sell_execution(
        event_key="stage-zero",
        chain="solana",
        token_address="MintRecovery111",
        symbol="RECOVER",
        stage=0,
        requested_raw=50,
        expected_output_raw=100,
    )
    store.complete_auto_sell_execution(
        event_key="stage-zero", signature="sell-stage-zero", next_stage=1
    )
    store.arm_auto_sell("MintRecovery111", reset_stage=True)
    policy = store.load_auto_sell_policy("MintRecovery111")
    assert policy is not None
    assert policy["stage"] == 0
    assert policy["last_signature"] is None
    store.close()


def test_confirmed_rebuy_uses_shared_budget_and_rearms_new_cycle(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rebuy-execution.db"
    wallet = str(Keypair().pubkey())
    store = SQLiteStore(str(path))
    config = settings(
        path,
        solana_wallet_address=wallet,
        auto_buy_enabled=True,
        auto_sell_enabled=True,
        auto_rebuy_enabled=True,
    )
    watch = store.start_auto_rebuy_watch(
        token_address="MintRecovery111",
        symbol="RECOVER",
        sell_signature="sell-1",
        exit_price_usd=1.0,
        exit_liquidity_usd=100_000,
        sale_proceeds_usdc_raw=10_000_000,
        sold_at_epoch=time.time() - 700,
        max_rebuys=1,
    )
    assert watch is not None
    for poll in range(3):
        watch = store.record_auto_rebuy_observation(
            token_address="MintRecovery111",
            current_price_usd=0.83 + poll * 0.01,
            observed_at_epoch=time.time() + poll,
            qualifying=True,
            confirmation_required=3,
            reason="recovery confirmed",
        )
    guard = LaunchGuard(config, store)

    class FakeRpc:
        async def token_balance(
            self, owner: str, mint: str
        ) -> SolanaTokenHolding:
            assert owner == wallet
            if mint == USDC_MINT:
                return SolanaTokenHolding(
                    mint=mint, amount=10, raw_amount=10_000_000, decimals=6
                )
            return SolanaTokenHolding(
                mint=mint, amount=0, raw_amount=0, decimals=6
            )

        async def mint_decimals(self, mint: str) -> int:
            assert mint == "MintRecovery111"
            return 6

    class FakeBuyer:
        async def preflight(
            self, intent: BuyIntent, _simulator: object
        ) -> BuyPreflightReceipt:
            return BuyPreflightReceipt(
                prepared=PreparedBuy(
                    intent=intent,
                    transaction="unsigned",
                    request_id="request-buy",
                    input_amount_raw=intent.amount_usdc_raw,
                    expected_output_raw=6_000_000,
                    minimum_output_raw=5_900_000,
                    price_impact_pct=1.0,
                    last_valid_block_height="123",
                ),
                units_consumed=100_000,
                log_count=5,
            )

        async def execute(self, prepared: PreparedBuy) -> BuyReceipt:
            return BuyReceipt(
                intent=prepared.intent,
                signature="buy-confirmed",
                input_amount_raw=prepared.input_amount_raw,
                output_amount_raw=6_000_000,
            )

    guard.auto_buyer = FakeBuyer()  # type: ignore[assignment]
    quote = MarketQuote(
        mint="MintRecovery111",
        symbol="RECOVER",
        price_sol=0.001,
        price_usd=0.85,
        liquidity_usd=90_000,
        market_cap_usd=200_000,
        pair_address="PairRecovery",
        pair_created_at_ms=1,
        buys_m5=14,
        sells_m5=5,
        volume_m5_usd=25_000,
        price_change_m5_pct=4,
    )

    asyncio.run(guard._execute_auto_rebuy(watch, quote, FakeRpc()))  # type: ignore[arg-type]

    completed = store.load_auto_rebuy_watch("MintRecovery111")
    assert completed is not None
    assert completed["status"] == "BOUGHT"
    assert completed["completed_rebuys"] == 1
    status = store.auto_buy_status()
    assert status["fund"]["seed_buys_used"] == 1
    assert status["positions"][0]["status"] == "OPEN"
    holding = next(
        item
        for item in store.load_owned_holdings("solana")
        if item.token_address == "MintRecovery111"
    )
    assert holding.cost_amount == 5
    policy = store.load_auto_sell_policy("MintRecovery111")
    assert policy is not None
    assert policy["armed"] == 1
    assert policy["stage"] == 0
    store.close()


def test_portfolio_advisor_uses_cost_basis_for_partial_profit() -> None:
    holding = OwnedHolding(
        chain="solana",
        token_address="MintOwned111",
        symbol="OWN",
        quantity=20_000,
        entry_price=0.000001,
        price_currency="SOL",
        cost_amount=0.02,
    )
    quote = MarketQuote(
        mint=holding.token_address,
        symbol=holding.symbol,
        price_sol=0.00000135,
        price_usd=0.0002,
        chain="solana",
        liquidity_usd=50_000,
        market_cap_usd=100_000,
        pair_address="PairOwned",
        pair_created_at_ms=1,
        buys_m5=20,
        sells_m5=21,
        volume_m5_usd=5_000,
        price_change_m5_pct=-1,
    )

    signal = PortfolioAdvisor().evaluate(holding, quote, sol_usd=150)

    assert signal.decision == "TAKE PARTIAL"
    assert signal.pnl_pct == pytest.approx(35)
    assert signal.current_value_usd == pytest.approx(4.0)


def test_portfolio_advisor_protects_a_small_gain_reversing_before_the_big_trailing_stop() -> None:
    """A gain too small to reach the 20%/12% trailing-stop tier still
    deserves protecting once it's genuinely reversing - otherwise a token
    that peaks well above this bar and gives it back before ever reaching
    the bigger threshold captures nothing (observed live: a +35.5% peak
    round-tripped into a realized loss with no protective action taken
    along the way)."""
    holding = OwnedHolding(
        chain="solana", token_address="MintSmallGain111", symbol="SMOL",
        quantity=30_000, entry_price=0.0001, price_currency="USD",
    )

    def quote_at(price: float, *, change: float) -> MarketQuote:
        return MarketQuote(
            mint=holding.token_address, symbol=holding.symbol,
            price_sol=price / 150, price_usd=price, chain="solana",
            liquidity_usd=50_000, market_cap_usd=100_000,
            pair_address="PairSmallGain", pair_created_at_ms=1,
            buys_m5=10, sells_m5=15, volume_m5_usd=5_000,
            price_change_m5_pct=change,
        )

    advisor = PortfolioAdvisor()
    # Peak at +35% - establishes the tracked peak.
    advisor.evaluate(holding, quote_at(0.000135, change=20))
    # Pulled back to +10% (well below the 20% big-tier activation bar) with
    # falling momentum and net selling - real reversal evidence.
    signal = advisor.evaluate(holding, quote_at(0.00011, change=-3))

    assert signal.pnl_pct == pytest.approx(10, abs=0.5)
    assert signal.decision == "PROTECT PROFIT"
    assert "reversing" in signal.reason


def test_portfolio_advisor_does_not_protect_a_small_gain_without_reversal_evidence() -> None:
    """A small pullback with no confirming evidence (momentum still firm,
    no net selling) is ordinary noise, not a reversal - must not fire the
    more sensitive small-gain tier."""
    holding = OwnedHolding(
        chain="solana", token_address="MintSmallGainNoise111", symbol="NOISE",
        quantity=30_000, entry_price=0.0001, price_currency="USD",
    )

    def quote_at(price: float, *, change: float, buys: int, sells: int) -> MarketQuote:
        return MarketQuote(
            mint=holding.token_address, symbol=holding.symbol,
            price_sol=price / 150, price_usd=price, chain="solana",
            liquidity_usd=50_000, market_cap_usd=100_000,
            pair_address="PairSmallGainNoise", pair_created_at_ms=1,
            buys_m5=buys, sells_m5=sells, volume_m5_usd=5_000,
            price_change_m5_pct=change,
        )

    advisor = PortfolioAdvisor()
    advisor.evaluate(holding, quote_at(0.000135, change=20, buys=20, sells=5))
    signal = advisor.evaluate(holding, quote_at(0.00011, change=3, buys=20, sells=5))

    assert signal.pnl_pct == pytest.approx(10, abs=0.5)
    assert signal.decision != "PROTECT PROFIT"


def test_portfolio_advisor_keeps_raw_decision_when_value_crashes_below_sell_floor() -> None:
    """A position that crashes below the $2 sell floor still reads as HOLD
    for notifications (unchanged), but `raw_decision` must keep the true
    risk-based call so live-trial execution can still liquidate it."""
    holding = OwnedHolding(
        chain="solana",
        token_address="MintCrashed111",
        symbol="CRASH",
        quantity=1_000,
        entry_price=0.01,
        price_currency="USD",
    )
    quote = MarketQuote(
        mint=holding.token_address,
        symbol=holding.symbol,
        price_sol=0.0000001,
        price_usd=0.001,
        chain="solana",
        liquidity_usd=50_000,
        market_cap_usd=10_000,
        pair_address="PairCrashed",
        pair_created_at_ms=1,
        buys_m5=10,
        sells_m5=5,
        volume_m5_usd=1_000,
        price_change_m5_pct=0,
    )

    signal = PortfolioAdvisor().evaluate(holding, quote)

    assert signal.current_value_usd == pytest.approx(1.0)
    assert signal.decision == "HOLD"
    assert "sell minimum" in signal.reason
    assert signal.raw_decision == "EXIT WARNING"


def test_portfolio_advisor_warns_on_momentum_reversal_without_cost_basis() -> None:
    holding = OwnedHolding(
        chain="solana",
        token_address="MintRisk111",
        symbol="RISK",
        quantity=20_000,
    )
    quote = MarketQuote(
        mint=holding.token_address,
        symbol=holding.symbol,
        price_sol=0.000001,
        price_usd=0.00015,
        chain="solana",
        liquidity_usd=10_000,
        market_cap_usd=50_000,
        pair_address="PairRisk",
        pair_created_at_ms=1,
        buys_m5=5,
        sells_m5=10,
        volume_m5_usd=4_000,
        price_change_m5_pct=-9,
    )

    signal = PortfolioAdvisor().evaluate(holding, quote)

    assert signal.decision == "EXIT WARNING"
    assert signal.pnl_pct is None
    assert "seller/buyer pressure" in signal.reason


def test_owned_holding_persists_and_dashboard_is_read_only(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(str(tmp_path / "portfolio.db"))
    holding = OwnedHolding(
        chain="solana",
        token_address="MintOwned111",
        symbol="OWN",
        quantity=125,
        entry_price=0.01,
        price_currency="USD",
        cost_amount=1.25,
    )
    store.save_owned_holding(holding)
    updated = OwnedHolding(
        chain="solana",
        token_address="MintOwned111",
        symbol="OWN",
        quantity=150,
        entry_price=0.009,
        price_currency="USD",
        cost_amount=1.35,
    )
    store.save_owned_holding(updated)

    assert store.load_owned_holdings("solana") == [updated]
    signal = PortfolioAdvisor().evaluate(updated, None)
    output = format_portfolio_dashboard(
        build_portfolio_snapshot(
            [signal], wallet="Wallet111", poll_seconds=15
        ),
        color=False,
        show_all=True,
    )
    assert "MY HOLDINGS (READ-ONLY)" in output
    assert "UNPRICED" in output
    assert "token=MintOwned111" in output
    store.save_portfolio_state(
        chain="solana",
        token_address="MintOwned111",
        peak_price=0.02,
        baseline_liquidity_usd=50_000,
    )
    assert store.load_portfolio_states() == [
        {
            "chain": "solana",
            "token_address": "MintOwned111",
            "peak_price": 0.02,
            "baseline_liquidity_usd": 50_000.0,
            "below_sell_minimum": 0,
        }
    ]
    store.close()


def test_phone_notifications_deduplicate_and_respect_cooldown(
    tmp_path: Path,
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] = []

        async def send(self, **payload: object) -> str:
            self.messages.append(payload)
            return f"request-{len(self.messages)}"

    initial = market_quote(
        liquidity=50_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=15,
    )
    book = RecommendationBook(
        pool_size=10, ttl_seconds=60, entry_confirmation_polls=1
    )
    candidate = book.add(initial, CoinIntelligence().score(initial), now=0)
    assert candidate is not None
    assert candidate.decision == "WAIT FOR PULLBACK"

    store = SQLiteStore(tmp_path / "notifications.db")
    client = FakeClient()
    clock = [1_000.0]
    notifier = DecisionNotifier(
        client=client,
        store=store,
        decisions=("WAIT FOR PULLBACK", "BUY ZONE"),
        min_score=60,
        cooldown_seconds=300,
        clock=lambda: clock[0],
    )

    assert asyncio.run(notifier.maybe_send(candidate)) is True
    assert asyncio.run(notifier.maybe_send(candidate)) is False
    assert len(client.messages) == 1
    assert client.messages[0]["priority"] == 0

    touches_low = MarketQuote(
        mint=initial.mint,
        symbol=initial.symbol,
        price_sol=initial.price_sol * 0.94,
        liquidity_usd=initial.liquidity_usd,
        market_cap_usd=initial.market_cap_usd,
        pair_address=initial.pair_address,
        pair_created_at_ms=initial.pair_created_at_ms,
        buys_m5=25,
        sells_m5=25,
        volume_m5_usd=16_000,
        price_change_m5_pct=-3,
    )
    book.update(touches_low, now=3)

    pullback = MarketQuote(
        mint=initial.mint,
        symbol=initial.symbol,
        price_sol=initial.price_sol * 0.96,
        liquidity_usd=initial.liquidity_usd,
        market_cap_usd=initial.market_cap_usd,
        pair_address=initial.pair_address,
        pair_created_at_ms=initial.pair_created_at_ms,
        buys_m5=40,
        sells_m5=20,
        volume_m5_usd=18_000,
        price_change_m5_pct=2,
    )
    book.update(pullback, now=5)
    assert candidate.decision == "BUY ZONE"

    clock[0] = 1_200
    assert asyncio.run(notifier.maybe_send(candidate)) is True
    assert len(client.messages) == 2
    assert client.messages[1]["priority"] == 1
    assert store.last_notification("pushover", candidate.key) == (
        "BUY ZONE",
        1_200,
    )

    above_zone = MarketQuote(
        mint=initial.mint,
        symbol=initial.symbol,
        price_sol=initial.price_sol,
        liquidity_usd=initial.liquidity_usd,
        market_cap_usd=initial.market_cap_usd,
        pair_address=initial.pair_address,
        pair_created_at_ms=initial.pair_created_at_ms,
        buys_m5=40,
        sells_m5=20,
        volume_m5_usd=10_000,
        price_change_m5_pct=4,
    )
    book.update(above_zone, now=6)
    assert candidate.decision == "WAIT FOR PULLBACK"
    clock[0] = 1_300
    assert asyncio.run(notifier.maybe_send(candidate)) is False
    clock[0] = 1_501
    assert asyncio.run(notifier.maybe_send(candidate)) is True
    assert len(client.messages) == 3
    asyncio.run(notifier.send_high_priority_test())
    assert len(client.messages) == 4
    assert client.messages[3]["priority"] == 1
    assert client.messages[3]["sound"] == "siren"
    store.close()


def test_portfolio_phone_alert_is_high_priority_and_state_deduplicated(
    tmp_path: Path,
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] = []

        async def send(self, **payload: object) -> str:
            self.messages.append(payload)
            return f"request-{len(self.messages)}"

    holding = OwnedHolding(
        chain="solana",
        token_address="MintAlert111",
        symbol="ALERT",
        quantity=20_000,
    )
    quote = MarketQuote(
        mint=holding.token_address,
        symbol=holding.symbol,
        price_sol=0.000001,
        price_usd=0.00015,
        chain="solana",
        liquidity_usd=10_000,
        market_cap_usd=50_000,
        pair_address="PairAlert",
        pair_created_at_ms=1,
        buys_m5=5,
        sells_m5=10,
        volume_m5_usd=4_000,
        price_change_m5_pct=-9,
    )
    signal = PortfolioAdvisor().evaluate(holding, quote)
    assert signal.decision == "EXIT WARNING"

    store = SQLiteStore(tmp_path / "portfolio-alerts.db")
    client = FakeClient()
    notifier = PortfolioNotifier(
        client=client,
        store=store,
        decisions=("TAKE PARTIAL", "PROTECT PROFIT", "EXIT WARNING"),
        high_priority_decisions=(
            "TAKE PARTIAL",
            "PROTECT PROFIT",
            "EXIT WARNING",
        ),
        cooldown_seconds=300,
        clock=lambda: 1_000,
    )

    assert asyncio.run(notifier.maybe_send(signal)) is True
    assert asyncio.run(notifier.maybe_send(signal)) is False
    assert len(client.messages) == 1
    assert client.messages[0]["priority"] == 1
    title, message, sound = format_portfolio_notification(signal)
    assert "REVIEW SELL" in title
    assert "no order was placed" in message
    assert sound == "siren"
    store.close()


def test_phone_notification_explains_signal() -> None:
    quote = market_quote(
        liquidity=50_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=15,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60)
    candidate = book.add(quote, CoinIntelligence().score(quote), now=0)
    assert candidate is not None

    title, message, sound = format_candidate_notification(candidate)

    assert "WAIT FOR PULLBACK" in title
    assert "Entry zone:" in message
    assert "Reason:" in message
    assert candidate.symbol in message
    assert sound == "pushover"


def test_robinhood_quote_uses_usd_and_exact_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    address = "0x5d102d1E69E77591D486aa4f663B3645151BBdf6"
    oracle = DexScreenerOracle()
    monkeypatch.setattr(
        oracle,
        "_request_token",
        lambda _address: [
            {
                "chainId": "robinhood",
                "pairAddress": "0xPair",
                "baseToken": {"address": address.lower(), "symbol": "VOLT"},
                "quoteToken": {"address": "0xQuote", "symbol": "WETH"},
                "priceNative": "0.0001",
                "priceUsd": "0.25",
                "liquidity": {"usd": 50000},
                "marketCap": 100000,
                "txns": {"m5": {"buys": 60, "sells": 20}},
                "volume": {"m5": 15000},
                "priceChange": {"m5": 15},
                "pairCreatedAt": 1,
            }
        ],
    )

    quote = oracle._fetch(address, "robinhood")

    assert quote is not None
    assert quote.chain == "robinhood"
    assert quote.price_sol == 0
    assert quote.price_usd == pytest.approx(0.25)
    assert quote.recommendation_key.endswith(address.lower())


def test_oracle_retries_a_429_and_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 used to be swallowed identically to "no pairs for this token" -
    observed live against the real API for a token that was actively
    trading with real liquidity. A couple of short backoff retries should
    clear a transient rate limit instead of reporting no quote at all."""
    mint = "BLSuVTxKYDL4vm4XmG3oEJfsSGfJZF3cgy98ri68pump"
    body = (
        '{"pairs":[{"chainId":"solana",'
        f'"baseToken":{{"address":"{mint}"}},'
        '"quoteToken":{"address":"So11111111111111111111111111111111111111112"},'
        '"priceNative":"0.0001","priceUsd":"0.25","liquidity":{"usd":50000},'
        '"marketCap":100000,"txns":{"m5":{"buys":10,"sells":5}},'
        '"volume":{"m5":1000},"priceChange":{"m5":1},"pairCreatedAt":1}]}'
    ).encode()

    calls = []

    def flaky_urlopen(*_args: object, **_kwargs: object) -> object:
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.HTTPError(
                "https://api.dexscreener.com/latest/dex/tokens/x", 429,
                "Too Many Requests", None, io.BytesIO(b""),
            )
        return io.BytesIO(body)

    monkeypatch.setattr("urllib.request.urlopen", flaky_urlopen)
    monkeypatch.setattr("solana_launch_guard.market.time.sleep", lambda _seconds: None)
    oracle = DexScreenerOracle()

    pairs = oracle._request_token(mint)

    assert len(calls) == 3
    assert len(pairs) == 1
    assert pairs[0]["priceUsd"] == "0.25"


def test_oracle_does_not_retry_a_non_429_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def reject(*_args: object, **_kwargs: object) -> object:
        calls.append(1)
        raise urllib.error.HTTPError(
            "https://api.dexscreener.com/latest/dex/tokens/x", 500,
            "Server Error", None, io.BytesIO(b""),
        )

    monkeypatch.setattr("urllib.request.urlopen", reject)
    monkeypatch.setattr("solana_launch_guard.market.time.sleep", lambda _seconds: None)
    oracle = DexScreenerOracle()

    pairs = oracle._request_token("A" * 44)

    assert pairs == []
    assert len(calls) == 1


def test_oracle_with_zero_retries_gives_up_on_first_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bulk portfolio/watchlist scanner constructs with max_429_retries=0
    (app.py) so a rate limit costs one failed call, not a blocking retry
    sleep repeated across dozens of concurrently-scanned holdings - that
    compounding sleep was what pushed the portfolio snapshot stale enough
    to block live buy/sell decisions that depend on its freshness."""
    calls = []
    slept = []

    def reject_429(*_args: object, **_kwargs: object) -> object:
        calls.append(1)
        raise urllib.error.HTTPError(
            "https://api.dexscreener.com/latest/dex/tokens/x", 429,
            "Too Many Requests", None, io.BytesIO(b""),
        )

    monkeypatch.setattr("urllib.request.urlopen", reject_429)
    monkeypatch.setattr("solana_launch_guard.market.time.sleep", lambda seconds: slept.append(seconds))
    oracle = DexScreenerOracle(max_429_retries=0)

    pairs = oracle._request_token("A" * 44)

    assert pairs == []
    assert len(calls) == 1
    assert slept == []


def test_robinhood_profiles_and_stock_contracts_are_discovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meme = "0x1111111111111111111111111111111111111111"
    stock = "0x2222222222222222222222222222222222222222"
    oracle = DexScreenerOracle()

    def fake_request(url: str) -> object:
        if url.endswith("/rhj/assets"):
            return {
                "assets": [
                    {
                        "tokenSymbol": "NVDA",
                        "deployments": [
                            {"contractAddress": stock, "chainId": 4663}
                        ]
                    }
                ]
            }
        return [
            {"chainId": "robinhood", "tokenAddress": meme},
            {"chainId": "solana", "tokenAddress": "SolMint"},
        ]

    monkeypatch.setattr(oracle, "_request_json", fake_request)

    assert oracle._discover_token_profiles("robinhood") == (meme,)
    assert oracle._robinhood_stock_token_addresses() == frozenset(
        {stock.casefold()}
    )
    assert oracle._robinhood_stock_token_symbols() == frozenset({"nvda"})


def test_stock_symbols_and_wrapped_stock_symbols_are_excluded() -> None:
    symbols = frozenset({"nvda", "spy"})

    assert _is_stock_token_symbol("NVDA", symbols) is True
    assert _is_stock_token_symbol("wNVDAx", symbols) is True
    assert _is_stock_token_symbol("MEME", symbols) is False


def test_xstocks_suffix_symbols_are_excluded() -> None:
    """Backed Finance's xStocks convention on Solana is a bare "x" suffix
    with no "w" prefix (e.g. NVDAx for NVIDIA) - confirmed against the
    actual on-chain token, distinct from the wrapped w...x pattern.
    """
    symbols = frozenset({"nvda", "spy"})

    assert _is_stock_token_symbol("NVDAx", symbols) is True
    assert _is_stock_token_symbol("nvdax", symbols) is True
    assert _is_stock_token_symbol("SOLx", symbols) is False


def test_restored_pullback_candidate_that_is_a_tokenized_stock_is_purged(
    tmp_path: Path,
) -> None:
    """PullbackTracker.restore() runs synchronously in LaunchGuard.__init__,
    before the Robinhood stock-symbol registry can be fetched, so a
    candidate persisted to disk before the stock exclusion existed (or
    before this run) can still be sitting in guard.recommendations after
    construction. The exclusion must also be applied once the registry is
    available, at the start of run(), to whatever restore() already
    loaded.
    """
    database = tmp_path / "restored-stock-candidate.db"
    snapshot_path = tmp_path / "recommendations.json"
    config = settings(database, recommendation_snapshot_path=str(snapshot_path))

    now = time.time()
    seed_book = RecommendationBook(ttl_seconds=1800)
    stock_candidate = RecommendationCandidate(
        mint="Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh", symbol="NVDAx",
        chain="solana", tier="CORE", intelligence_score=90,
        initial_price=224.85, current_price=224.85, price_currency="USD",
        liquidity_usd=60_000, initial_liquidity_usd=60_000,
        volume_m5_usd=100, initial_volume_m5_usd=100, buys_m5=10, sells_m5=2,
        price_change_m5_pct=-1, buy_sell_ratio=5, observed_at=now, updated_at=now,
        peak_price=224.85, entry_zone_low=200, entry_zone_high=230,
        decision="WATCH", decision_reason="waiting for price and buyer confirmation",
        entry_confirmation_required=3,
    )
    seed_book.candidates[stock_candidate.key] = stock_candidate
    PullbackTracker(snapshot_path).record(seed_book, now=now)

    store = SQLiteStore(str(database))
    guard = LaunchGuard(config, store)
    assert stock_candidate.key in guard.recommendations.candidates

    class FakeOracle:
        async def robinhood_stock_token_symbols(self) -> frozenset[str]:
            return frozenset({"nvda"})

    guard.oracle = FakeOracle()  # type: ignore[assignment]
    asyncio.run(guard._purge_restored_stock_token_candidates())

    assert stock_candidate.key not in guard.recommendations.candidates
    store.close()


def test_evm_transfer_of_a_tokenized_stock_is_not_added_as_a_candidate(
    tmp_path: Path,
) -> None:
    """run_multichain_feed already excludes tokenized stocks from becoming
    trading candidates - pullback/momentum sniping logic doesn't fit a real
    stock's price action. A wallet transfer of one (ordinary Robinhood
    brokerage activity reflected on-chain, not a launch to snipe) must not
    bypass that same exclusion.
    """
    database = tmp_path / "evm-transfer-stock.db"
    config = settings(database)
    store = SQLiteStore(str(database))
    guard = LaunchGuard(config, store)

    stock_quote = MarketQuote(
        mint="0xStockContract",
        symbol="NVDA",
        price_sol=0,
        price_usd=0.25,
        chain="robinhood",
        liquidity_usd=50_000,
        market_cap_usd=100_000,
        pair_address="0xPair",
        pair_created_at_ms=1,
        buys_m5=60,
        sells_m5=20,
        volume_m5_usd=15_000,
        price_change_m5_pct=15,
    )

    class FakeOracle:
        async def quote(
            self, mint: str, *, chain: str = "solana"
        ) -> MarketQuote | None:
            return stock_quote

        async def robinhood_stock_token_symbols(self) -> frozenset[str]:
            return frozenset({"nvda"})

    guard.oracle = FakeOracle()  # type: ignore[assignment]
    transfer = EvmTransfer(
        chain="robinhood", wallet="0xWallet", transaction_hash="0xTx",
        block_number=1, log_index=0, contract="0xStockContract",
        symbol="NVDA", direction="BUY", token_amount=1,
    )

    asyncio.run(guard.handle_evm_transfer(transfer))

    assert stock_quote.recommendation_key not in guard.recommendations.candidates
    store.close()


def test_solana_candidate_that_is_a_tokenized_stock_is_not_added(
    tmp_path: Path,
) -> None:
    """The Robinhood-chain paths already exclude tokenized stocks from
    becoming trading candidates. A real-world equity tokenized on Solana
    under the same ticker deserves the identical exclusion - the symbol is
    chain-agnostic, and pullback/momentum sniping logic doesn't fit a real
    stock's price action no matter which chain it launched on.
    """
    database = tmp_path / "solana-stock-candidate.db"
    config = settings(database, intelligence_wait_seconds=0)
    store = SQLiteStore(str(database))
    guard = LaunchGuard(config, store)

    stock_quote = MarketQuote(
        mint="MintNvda111",
        symbol="NVDA",
        price_sol=0.001,
        price_usd=0.25,
        chain="solana",
        liquidity_usd=50_000,
        market_cap_usd=100_000,
        pair_address="PairNvda111",
        pair_created_at_ms=1,
        buys_m5=60,
        sells_m5=20,
        volume_m5_usd=15_000,
        price_change_m5_pct=15,
    )

    class FakeOracle:
        async def quote(
            self, mint: str, *, chain: str = "solana"
        ) -> MarketQuote | None:
            return stock_quote

        async def robinhood_stock_token_symbols(self) -> frozenset[str]:
            return frozenset({"nvda"})

        async def sol_usd_price(self) -> float:
            return 100.0

    guard.oracle = FakeOracle()  # type: ignore[assignment]
    launch = Launch.from_payload(
        launch_payload(mint=stock_quote.mint, symbol="NVDA")
    )

    asyncio.run(guard.evaluate_candidate(launch))

    assert stock_quote.recommendation_key not in guard.recommendations.candidates
    store.close()


def test_robinhood_recommendation_shows_contract_and_fomo_link() -> None:
    address = "0x3333333333333333333333333333333333333333"
    quote = MarketQuote(
        mint=address,
        symbol="RHCOIN",
        price_sol=0,
        price_usd=0.00025,
        chain="robinhood",
        liquidity_usd=50_000,
        market_cap_usd=100_000,
        pair_address="0xPair",
        pair_created_at_ms=1,
        buys_m5=60,
        sells_m5=20,
        volume_m5_usd=15_000,
        price_change_m5_pct=15,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60)
    book.add(quote, CoinIntelligence().score(quote), now=0)
    snapshot = build_snapshot(book.ranked(), pending_count=0, poll_seconds=15)

    output = format_dashboard(snapshot, color=False)

    assert "chain=RH" in output
    assert f"contract={address}" in output
    assert f"fomo=https://fomo.family/tokens/robinhood/{address}" in output
    assert "price=$0.00025" in output


def test_base_recommendation_uses_generic_evm_contract_display() -> None:
    address = "0x4444444444444444444444444444444444444444"
    quote = MarketQuote(
        mint=address,
        symbol="BASECOIN",
        price_sol=0,
        price_usd=0.05,
        chain="base",
        liquidity_usd=50_000,
        market_cap_usd=100_000,
        pair_address="0xPair",
        pair_created_at_ms=1,
        buys_m5=60,
        sells_m5=20,
        volume_m5_usd=15_000,
        price_change_m5_pct=15,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60)
    book.add(quote, CoinIntelligence().score(quote), now=0)

    output = format_dashboard(
        build_snapshot(book.ranked(), pending_count=0, poll_seconds=15),
        color=False,
    )

    assert "chain=BASE" in output
    assert f"contract={address}" in output
    assert f"market=https://dexscreener.com/base/{address}" in output


def test_bnb_recommendation_uses_bsc_market_slug() -> None:
    address = "0x5555555555555555555555555555555555555555"
    quote = MarketQuote(
        mint=address,
        symbol="BNBCOIN",
        price_sol=0,
        price_usd=0.05,
        chain="bsc",
        liquidity_usd=50_000,
        market_cap_usd=100_000,
        pair_address="0xPair",
        pair_created_at_ms=1,
        buys_m5=60,
        sells_m5=20,
        volume_m5_usd=15_000,
        price_change_m5_pct=15,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60)
    book.add(quote, CoinIntelligence().score(quote), now=0)

    output = format_dashboard(
        build_snapshot(book.ranked(), pending_count=0, poll_seconds=15),
        color=False,
    )

    assert "chain=BNB" in output
    assert f"market=https://dexscreener.com/bsc/{address}" in output


def test_evm_transfer_parser_reads_incoming_erc20(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wallet = "0x1111111111111111111111111111111111111111"
    contract = "0x2222222222222222222222222222222222222222"
    rpc = EvmRpc("https://example.invalid")
    calls = 0

    def fake_request(method: str, _params: list[object]) -> object:
        nonlocal calls
        assert method == "eth_getLogs"
        calls += 1
        if calls == 1:
            return [
                {
                    "transactionHash": "0xabc",
                    "address": contract,
                    "logIndex": "0x2",
                    "blockNumber": "0x64",
                    "data": hex(1_500_000),
                }
            ]
        return []

    async def fake_metadata(_contract: str) -> tuple[str, int]:
        return "TOK", 6

    monkeypatch.setattr(rpc, "_request", fake_request)
    monkeypatch.setattr(rpc, "token_metadata", fake_metadata)

    transfers = asyncio.run(
        rpc.transfers(
            chain="base", wallet=wallet, from_block=1, to_block=100
        )
    )

    assert len(transfers) == 1
    assert transfers[0].direction == "IN"
    assert transfers[0].symbol == "TOK"
    assert transfers[0].token_amount == pytest.approx(1.5)


def test_hypercore_parses_spot_perps_and_fills() -> None:
    async def ignore(_item: object) -> None:
        return None

    watcher = HyperCoreWatcher(
        wallet="0x1111111111111111111111111111111111111111",
        fill_callback=ignore,
        state_callback=ignore,
    )
    state = watcher._parse_state(
        {"balances": [{"coin": "HYPE", "total": "2.5"}]},
        {
            "assetPositions": [
                {"position": {"coin": "BTC", "szi": "0.01"}}
            ]
        },
    )
    fills = watcher._parse_fills(
        [
            {
                "tid": 123,
                "coin": "HYPE",
                "side": "B",
                "sz": "1.5",
                "px": "20",
                "time": 1000,
            }
        ]
    )

    assert state.spot_balances == (("HYPE", 2.5),)
    assert state.perp_positions == (("BTC", 0.01),)
    assert fills[0].fill_id == "123"
    assert fills[0].price == pytest.approx(20)


def test_multichain_wallet_events_are_deduplicated(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "wallet.db")
    values = {
        "chain": "base",
        "wallet": "0x1111111111111111111111111111111111111111",
        "event_id": "0xabc:2",
        "block_number": 100,
        "token_address": "0x2222222222222222222222222222222222222222",
        "symbol": "TOK",
        "direction": "IN",
        "token_amount": 1.5,
        "price_usd": 0.25,
        "source": "EVM_TRANSFER",
    }

    assert store.save_wallet_event(**values) is True
    assert store.save_wallet_event(**values) is False
    info = store.multichain_wallet_info(values["wallet"])

    assert info["chains"][0]["chain"] == "base"
    assert info["chains"][0]["events"] == 1
    assert info["recent"][0]["symbol"] == "TOK"
    store.close()


def strategy_position() -> object:
    from solana_launch_guard.core import Position

    return Position(
        mint="MintScore111",
        symbol="SCORE",
        entry_price_sol=1.0,
        quantity=10,
        cost_sol=10,
        take_profit_price_sol=51,
        stop_loss_price_sol=0.6,
        opened_at="now",
        latest_price_sol=1.0,
    )


def test_adaptive_strategy_trails_after_peak_gain() -> None:
    strategy = AdaptiveStrategy(
        trailing_activation_pct=20,
        trailing_stop_pct=12,
    )
    position = strategy_position()
    first = market_quote(
        liquidity=50_000,
        market_cap=100_000,
        buys=30,
        sells=15,
        volume=10_000,
        change=20,
    )
    first = MarketQuote(
        **{**first.__dict__, "price_sol": 1.3}
    ) if hasattr(first, "__dict__") else MarketQuote(
        mint=first.mint,
        symbol=first.symbol,
        price_sol=1.3,
        liquidity_usd=first.liquidity_usd,
        market_cap_usd=first.market_cap_usd,
        pair_address=first.pair_address,
        pair_created_at_ms=first.pair_created_at_ms,
        buys_m5=first.buys_m5,
        sells_m5=first.sells_m5,
        volume_m5_usd=first.volume_m5_usd,
        price_change_m5_pct=first.price_change_m5_pct,
    )
    strategy.register_open(position, first, "MOONSHOT")
    pullback = MarketQuote(
        mint=first.mint,
        symbol=first.symbol,
        price_sol=1.1,
        liquidity_usd=50_000,
        market_cap_usd=100_000,
        pair_address="Pair111",
        pair_created_at_ms=1,
        buys_m5=20,
        sells_m5=15,
        volume_m5_usd=8_000,
        price_change_m5_pct=-2,
    )

    decision = strategy.evaluate_open(position, pullback)

    assert decision.action == "SELL"
    assert "TRAILING_STOP" in decision.reason


def test_adaptive_strategy_requires_recovery_for_reentry() -> None:
    strategy = AdaptiveStrategy(reentry_cooldown_seconds=0)
    position = strategy_position()
    entry_quote = MarketQuote(
        mint="MintScore111",
        symbol="SCORE",
        price_sol=1.0,
        liquidity_usd=50_000,
        market_cap_usd=100_000,
        pair_address="Pair111",
        pair_created_at_ms=1,
        buys_m5=20,
        sells_m5=10,
        volume_m5_usd=10_000,
        price_change_m5_pct=5,
    )
    strategy.register_open(position, entry_quote, "CORE")
    strategy.record_exit(position.mint, 0.9)
    recovery = MarketQuote(
        mint=entry_quote.mint,
        symbol=entry_quote.symbol,
        price_sol=0.95,
        liquidity_usd=48_000,
        market_cap_usd=95_000,
        pair_address="Pair111",
        pair_created_at_ms=1,
        buys_m5=30,
        sells_m5=10,
        volume_m5_usd=12_000,
        price_change_m5_pct=6,
    )

    decision = strategy.evaluate_reentry(recovery)

    assert decision.action == "REENTER"
    assert "RECOVERY" in decision.reason


def test_recommendation_refresh_extends_ttl_and_state_round_trips() -> None:
    quote = market_quote(
        liquidity=60_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=5,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=10)
    candidate = book.add(quote, CoinIntelligence().score(quote), now=100)
    assert candidate is not None

    book.update(quote, now=108)
    book.expire(now=111)
    assert book.ranked() == [candidate]

    payload = candidate.to_json()
    restored_book = RecommendationBook(pool_size=10, ttl_seconds=10)
    restored = type(candidate).from_json(payload)
    assert restored_book.restore(restored)
    assert restored_book.ranked()[0].entry_confirmation_count == 2

    restored_book.expire(now=119)
    assert restored_book.ranked() == []


def test_discovery_candidates_use_wall_clock_without_manual_timestamp(
    tmp_path: Path,
) -> None:
    quote = market_quote(
        liquidity=60_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=5,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60)
    candidate = book.add(quote, CoinIntelligence().score(quote))
    assert candidate is not None
    book.update(quote)
    book.update(quote)
    config = settings(
        tmp_path / "wall-clock.db",
        auto_buy_enabled=True,
        auto_buy_discovery=True,
    )

    assert candidate.decision == "EARLY BUY"
    assert auto_buy_discovery_rejection(candidate, config) is None


def test_auto_buy_discovery_watch_persists_and_evicts_weakest(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(str(tmp_path / "watch-capacity.db"))
    observed = time.time()
    first = store.start_auto_buy_discovery_watch(
        token_address="MintWatchOne",
        symbol="ONE",
        launch_payload=launch_payload(mint="MintWatchOne", symbol="ONE"),
        first_seen_epoch=observed,
        first_check_epoch=observed,
        expires_at_epoch=observed + 3600,
        max_active=1,
    )
    second = store.start_auto_buy_discovery_watch(
        token_address="MintWatchTwo",
        symbol="TWO",
        launch_payload=launch_payload(mint="MintWatchTwo", symbol="TWO"),
        first_seen_epoch=observed + 1,
        first_check_epoch=observed + 1,
        expires_at_epoch=observed + 3601,
        max_active=1,
    )

    assert first == "CREATED"
    assert second == "CREATED"
    assert store.load_auto_buy_discovery_watch("MintWatchOne")["status"] == (
        "EVICTED"
    )
    assert store.load_auto_buy_discovery_watch("MintWatchTwo")["status"] == (
        "WATCHING"
    )
    status = store.auto_buy_discovery_status()
    assert [item["token_address"] for item in status["active"]] == [
        "MintWatchTwo"
    ]
    assert status["recent_terminal"][0]["last_reason"] == (
        "active discovery watch capacity reached"
    )
    store.close()


def test_persistent_discovery_retries_missing_quote_and_restores_candidate(
    tmp_path: Path,
) -> None:
    database = tmp_path / "persistent-discovery.db"
    config = settings(
        database,
        auto_buy_enabled=True,
        auto_buy_discovery=True,
        auto_buy_watch_max_seconds=3600,
        auto_buy_watch_retry_base_seconds=5,
        auto_buy_watch_retry_max_seconds=60,
    )
    store = SQLiteStore(str(database))
    guard = LaunchGuard(config, store)
    quote = market_quote(
        liquidity=60_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=5,
    )

    class FakeOracle:
        def __init__(self) -> None:
            self.quotes: list[MarketQuote | None] = [None, quote]

        async def quote(
            self, mint: str, *, chain: str = "solana"
        ) -> MarketQuote | None:
            assert mint == quote.mint
            assert chain == "solana"
            return self.quotes.pop(0)

        async def sol_usd_price(self) -> float:
            return 100.0

    guard.oracle = FakeOracle()  # type: ignore[assignment]
    payload = launch_payload(mint=quote.mint, symbol=quote.symbol)
    asyncio.run(guard.handle_launch(payload))
    watch = store.load_auto_buy_discovery_watch(quote.mint)
    assert watch is not None
    assert watch["status"] == "WATCHING"

    asyncio.run(guard._evaluate_auto_buy_discovery_watch(watch))
    watch = store.load_auto_buy_discovery_watch(quote.mint)
    assert watch is not None
    assert watch["attempts"] == 1
    assert watch["quote_failures"] == 1
    assert watch["status"] == "WATCHING"

    asyncio.run(guard._evaluate_auto_buy_discovery_watch(watch))
    watch = store.load_auto_buy_discovery_watch(quote.mint)
    assert watch is not None
    assert watch["attempts"] == 2
    assert watch["status"] == "TRACKING"
    assert watch["candidate_json"]
    assert quote.recommendation_key in guard.recommendations.candidates
    store.close()

    reopened = SQLiteStore(str(database))
    restored_guard = LaunchGuard(config, reopened)
    assert quote.recommendation_key in restored_guard.recommendations.candidates
    reopened.close()
