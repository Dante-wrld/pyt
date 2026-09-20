from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Any

from .wallet import SolanaRpc

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@dataclass(frozen=True, slots=True)
class UsdcBasisRecovery:
    mint: str
    wallet: str
    current_raw: int
    decimals: int
    acquired_raw: int
    spent_usdc_raw: int
    signatures: tuple[str, ...]

    @property
    def matches_current_holding(self) -> bool:
        return self.current_raw > 0 and self.current_raw == self.acquired_raw

    @property
    def cost_usd(self) -> float:
        return self.spent_usdc_raw / 1_000_000

    @property
    def quantity(self) -> float:
        return self.current_raw / 10**self.decimals

    @property
    def entry_price_usd(self) -> float:
        return self.cost_usd / self.quantity


def _raw_delta(transaction: dict[str, Any], wallet: str, mint: str) -> int:
    meta = transaction.get("meta") or {}
    def totals(name: str) -> int:
        value = 0
        for item in meta.get(name) or []:
            try:
                if item.get("owner") == wallet and item.get("mint") == mint:
                    value += int(item["uiTokenAmount"]["amount"])
            except (KeyError, TypeError, ValueError):
                continue
        return value
    return totals("postTokenBalances") - totals("preTokenBalances")


async def recover_usdc_basis(
    rpc: SolanaRpc, *, wallet: str, mint: str, limit: int = 150
) -> UsdcBasisRecovery | None:
    """Recover basis only from unambiguous USDC-funded net acquisitions.

    This deliberately refuses SOL-funded swaps, transfers, partial sales, and
    incomplete history. Those need user records or a dedicated tax indexer.
    """
    balance = await rpc.token_balance(wallet, mint)
    if balance.raw_amount <= 0:
        return None
    history = await rpc.signatures_for_address(wallet, limit=limit)
    acquired = 0
    spent = 0
    signatures: list[str] = []
    semaphore = asyncio.Semaphore(8)
    async def fetch(row: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        signature = str(row.get("signature") or "")
        if not signature or row.get("err") is not None:
            return signature, None
        async with semaphore:
            return signature, await rpc.get_transaction(signature)
    for signature, transaction in await asyncio.gather(*(fetch(row) for row in history)):
        if transaction is None or (transaction.get("meta") or {}).get("err") is not None:
            continue
        token_delta = _raw_delta(transaction, wallet, mint)
        usdc_delta = _raw_delta(transaction, wallet, USDC_MINT)
        if token_delta > 0 and usdc_delta < 0:
            acquired += token_delta
            spent += -usdc_delta
            signatures.append(signature)
        elif token_delta != 0:
            # Any transfer, SOL funded swap, or sale makes this partial history
            # unsuitable for a reliable average cost.
            return None
    if acquired <= 0 or spent <= 0 or not math.isfinite(spent):
        return None
    return UsdcBasisRecovery(
        mint=mint, wallet=wallet, current_raw=balance.raw_amount,
        decimals=balance.decimals, acquired_raw=acquired,
        spent_usdc_raw=spent, signatures=tuple(signatures),
    )
