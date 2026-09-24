"""Offline signature verification and bounded sell requote behavior."""
import asyncio
import base64
import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from solana_launch_guard import execution


def transaction(wallet, other=None, *, sign_other=False):
    accounts = [AccountMeta(wallet.pubkey(), True, True)]
    if other is not None:
        accounts.append(AccountMeta(other.pubkey(), True, False))
    message = MessageV0.try_compile(
        wallet.pubkey(),
        [Instruction(Keypair().pubkey(), b"", accounts)],
        [],
        Hash.default(),
    )
    signatures = [Signature.default()] * message.header.num_required_signatures
    if other is not None and sign_other:
        position = message.account_keys.index(other.pubkey())
        signatures[position] = other.sign_message(execution.to_bytes_versioned(message))
    return base64.b64encode(bytes(VersionedTransaction.populate(message, signatures))).decode()


def make_signer(monkeypatch, wallet):
    monkeypatch.setattr(execution.keyring, "get_password", lambda *_: "synthetic")
    monkeypatch.setattr(execution, "parse_solana_keypair", lambda _: wallet)
    return execution.KeyringSolanaSigner(expected_public_key=str(wallet.pubkey()))


def test_missing_additional_signature_is_detected_before_rpc(monkeypatch):
    wallet, other = Keypair(), Keypair()
    signer = make_signer(monkeypatch, wallet)
    with pytest.raises(execution.AdditionalSignerError, match="additional signature"):
        signer.sign(transaction(wallet, other))
    signed = signer.sign(transaction(wallet, other, sign_other=True))
    assert all(VersionedTransaction.from_bytes(base64.b64decode(signed)).verify_with_results())


def test_single_signer_quote_is_signed_and_verified(monkeypatch):
    wallet = Keypair()
    signed = make_signer(monkeypatch, wallet).sign(transaction(wallet))
    assert all(VersionedTransaction.from_bytes(base64.b64decode(signed)).verify_with_results())


def test_sell_requotes_bad_additional_signer_without_bypassing_guards(monkeypatch):
    wallet, other = Keypair(), Keypair()
    signer = make_signer(monkeypatch, wallet)
    calls = []

    class Client:
        async def order(self, **kwargs):
            calls.append(kwargs["exclude_routers"])
            bad = len(calls) == 1
            return {
                "inputMint": "synthetic-mint", "outputMint": execution.USDC_MINT,
                "inAmount": "10", "outAmount": "4000000",
                "otherAmountThreshold": "3900000", "transaction": transaction(wallet, other) if bad else transaction(wallet),
                "requestId": "synthetic", "router": "okx" if bad else "metis",
                "priceImpact": "-1.0", "slippageBps": 250,
            }

    class Simulator:
        async def simulate_transaction(self, signed):
            assert all(VersionedTransaction.from_bytes(base64.b64decode(signed)).verify_with_results())
            return {"logs": [], "unitsConsumed": 100}

    intent = execution.SellIntent("synthetic-mint", "TEST", 0, "test", 10, 10, 6, 1, 1, None, "test")
    seller = execution.SolanaAutoSeller(client=Client(), signer=signer)
    receipt = asyncio.run(seller.preflight(intent, Simulator()))
    assert calls == [("jupiterz",), ("jupiterz", "okx")]
    assert receipt.prepared.router == "metis"


def test_requoted_sell_still_enforces_price_impact(monkeypatch):
    wallet, other = Keypair(), Keypair()
    signer = make_signer(monkeypatch, wallet)
    calls = []

    class Client:
        async def order(self, **kwargs):
            calls.append(kwargs["exclude_routers"])
            return {
                "inputMint": "synthetic-mint", "outputMint": execution.USDC_MINT,
                "inAmount": "10", "outAmount": "4000000", "otherAmountThreshold": "3900000",
                "transaction": transaction(wallet, other), "requestId": "synthetic",
                "router": "okx" if len(calls) == 1 else "metis",
                "priceImpact": "-1" if len(calls) == 1 else "-9",
            }

    class Simulator:
        async def simulate_transaction(self, _):
            pytest.fail("an unsafe requote must not reach simulation")

    intent = execution.SellIntent("synthetic-mint", "TEST", 0, "test", 10, 10, 6, 1, 1, None, "test")
    seller = execution.SolanaAutoSeller(client=Client(), signer=signer)
    with pytest.raises(execution.QuoteGuardError, match="price impact"):
        asyncio.run(seller.preflight(intent, Simulator()))
    assert calls == [("jupiterz",), ("jupiterz", "okx")]


def test_sponsored_metis_preflight_reports_gas_blocker_without_requote(monkeypatch):
    wallet, sponsor = Keypair(), Keypair()
    signer = make_signer(monkeypatch, wallet)
    calls = []

    class Client:
        async def order(self, **kwargs):
            calls.append(kwargs["exclude_routers"])
            return {
                "inputMint": "synthetic-mint", "outputMint": execution.USDC_MINT,
                "inAmount": "10", "outAmount": "4000000",
                "otherAmountThreshold": "3900000", "transaction": transaction(wallet, sponsor),
                "requestId": "synthetic", "router": "metis", "gasless": True,
                "signatureFeePayer": str(sponsor.pubkey()),
                "priceImpact": "-1.0", "slippageBps": 250,
            }

    class Simulator:
        async def simulate_transaction(self, _):
            pytest.fail("sponsored transaction must not reach simulation")

    intent = execution.SellIntent("synthetic-mint", "TEST", 0, "test", 10, 10, 6, 1, 1, None, "test")
    seller = execution.SolanaAutoSeller(client=Client(), signer=signer)
    with pytest.raises(execution.AdditionalSignerError, match="wallet's SOL balance"):
        asyncio.run(seller.preflight(intent, Simulator()))
    assert calls == [("jupiterz",)]


def test_buy_requotes_bad_additional_signer_without_bypassing_guards(monkeypatch):
    """Mirrors the sell-side requote - confirmed live 2026-09-24 (CATE):
    excluding JupiterZ alone wasn't always enough, and the buy path had no
    retry, so one candidate's route halted the entire trial instead of
    just being skipped."""
    wallet, other = Keypair(), Keypair()
    signer = make_signer(monkeypatch, wallet)
    calls = []

    class Client:
        async def order(self, **kwargs):
            calls.append(kwargs["exclude_routers"])
            bad = len(calls) == 1
            return {
                "inputMint": execution.USDC_MINT, "outputMint": "synthetic-mint",
                "inAmount": "5000000", "outAmount": "4000000",
                "otherAmountThreshold": "3900000",
                "transaction": transaction(wallet, other) if bad else transaction(wallet),
                "requestId": "synthetic", "router": "okx" if bad else "metis",
                "priceImpact": "-1.0", "slippageBps": 250,
            }

    class Simulator:
        async def simulate_transaction(self, signed):
            assert all(VersionedTransaction.from_bytes(base64.b64decode(signed)).verify_with_results())
            return {"logs": [], "unitsConsumed": 100}

    intent = execution.BuyIntent("synthetic-mint", "TEST", "event", 5_000_000, "test")
    buyer = execution.SolanaAutoBuyer(client=Client(), signer=signer)
    receipt = asyncio.run(buyer.preflight(intent, Simulator()))
    assert calls == [("jupiterz",), ("jupiterz", "okx")]
    assert receipt.prepared.router == "metis"


def test_requoted_buy_still_enforces_price_impact(monkeypatch):
    wallet, other = Keypair(), Keypair()
    signer = make_signer(monkeypatch, wallet)
    calls = []

    class Client:
        async def order(self, **kwargs):
            calls.append(kwargs["exclude_routers"])
            return {
                "inputMint": execution.USDC_MINT, "outputMint": "synthetic-mint",
                "inAmount": "5000000", "outAmount": "4000000", "otherAmountThreshold": "3900000",
                "transaction": transaction(wallet, other), "requestId": "synthetic",
                "router": "okx" if len(calls) == 1 else "metis",
                "priceImpact": "-1" if len(calls) == 1 else "-9",
            }

    class Simulator:
        async def simulate_transaction(self, _):
            pytest.fail("an unsafe requote must not reach simulation")

    intent = execution.BuyIntent("synthetic-mint", "TEST", "event", 5_000_000, "test")
    buyer = execution.SolanaAutoBuyer(client=Client(), signer=signer)
    with pytest.raises(execution.QuoteGuardError, match="price impact"):
        asyncio.run(buyer.preflight(intent, Simulator()))
    assert calls == [("jupiterz",), ("jupiterz", "okx")]


def test_sponsored_buy_preflight_reports_gas_blocker_without_requote(monkeypatch):
    wallet, sponsor = Keypair(), Keypair()
    signer = make_signer(monkeypatch, wallet)
    calls = []

    class Client:
        async def order(self, **kwargs):
            calls.append(kwargs["exclude_routers"])
            return {
                "inputMint": execution.USDC_MINT, "outputMint": "synthetic-mint",
                "inAmount": "5000000", "outAmount": "4000000",
                "otherAmountThreshold": "3900000", "transaction": transaction(wallet, sponsor),
                "requestId": "synthetic", "router": "metis", "gasless": True,
                "signatureFeePayer": str(sponsor.pubkey()),
                "priceImpact": "-1.0", "slippageBps": 250,
            }

    class Simulator:
        async def simulate_transaction(self, _):
            pytest.fail("sponsored transaction must not reach simulation")

    intent = execution.BuyIntent("synthetic-mint", "TEST", "event", 5_000_000, "test")
    buyer = execution.SolanaAutoBuyer(client=Client(), signer=signer)
    with pytest.raises(execution.AdditionalSignerError, match="wallet's SOL balance"):
        asyncio.run(buyer.preflight(intent, Simulator()))
    assert calls == [("jupiterz",)]
