import asyncio

from solana_launch_guard.cost_basis import recover_usdc_basis
from solana_launch_guard.cost_basis import USDC_MINT
from solana_launch_guard.wallet import SolanaTokenHolding


MINT = "Mint"
WALLET = "Wallet"


def tx(token_before, token_after, usdc_before, usdc_after):
    return {"meta": {"err": None, "preTokenBalances": [
        {"owner": WALLET, "mint": MINT, "uiTokenAmount": {"amount": str(token_before)}},
        {"owner": WALLET, "mint": USDC_MINT, "uiTokenAmount": {"amount": str(usdc_before)}},
    ], "postTokenBalances": [
        {"owner": WALLET, "mint": MINT, "uiTokenAmount": {"amount": str(token_after)}},
        {"owner": WALLET, "mint": USDC_MINT, "uiTokenAmount": {"amount": str(usdc_after)}},
    ]}}


class Rpc:
    async def token_balance(self, wallet, mint):
        return SolanaTokenHolding(mint, 3, 3_000_000, 6)
    async def signatures_for_address(self, wallet, limit):
        return [{"signature": "a"}, {"signature": "b"}]
    async def get_transaction(self, signature):
        return {"a": tx(0, 1_000_000, 10_000_000, 8_000_000),
                "b": tx(1_000_000, 3_000_000, 8_000_000, 5_000_000)}[signature]


def test_recovers_only_matching_usdc_purchases():
    found = asyncio.run(recover_usdc_basis(Rpc(), wallet=WALLET, mint=MINT))
    assert found is not None and found.matches_current_holding
    assert found.cost_usd == 5
    assert found.entry_price_usd == 5 / 3


def test_rejects_mixed_or_partial_history():
    class Mixed(Rpc):
        async def get_transaction(self, signature):
            if signature == "a":
                return tx(0, 1_000_000, 10_000_000, 8_000_000)
            return tx(1_000_000, 500_000, 8_000_000, 9_000_000)
    assert asyncio.run(recover_usdc_basis(Mixed(), wallet=WALLET, mint=MINT)) is None
