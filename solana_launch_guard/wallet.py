from __future__ import annotations

import asyncio
import json
import logging
import ssl
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import certifi
import websockets

LOGGER = logging.getLogger("solana_launch_guard")
LAMPORTS_PER_SOL = Decimal("1000000000")


class TransactionSimulationFailed(RuntimeError):
    """A signed transaction reverted during on-chain simulation - most
    often the AMM's own slippage-tolerance check (e.g. Solana program
    error 6001), a routine, market-condition-dependent failure rather
    than an infrastructure or logic problem. Broadcast never happened
    (this is simulation-only), so callers on the live-trial buy/sell
    preflight path treat it the same as any other pre-reservation
    ValueError: skip this attempt, retry next cycle, never halt the
    trial over it."""

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
IGNORED_MINTS = {
    "So11111111111111111111111111111111111111112",  # wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYDg1QhVg2u4NfrJkGg7QZp",   # USDT
}
TOKEN_PROGRAM_IDS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
)


@dataclass(frozen=True, slots=True)
class WalletTrade:
    wallet: str
    signature: str
    slot: int
    mint: str
    side: str
    token_delta: float
    native_sol_delta: float | None
    # Net USDC the wallet gained (+) or spent (-) in the same transaction.
    # CopyFomo trades in USDC, so this - not SOL - is its cost and proceeds.
    # None when the wallet's USDC balance entry could not be read.
    usdc_delta: float | None = None


@dataclass(frozen=True, slots=True)
class SolanaTokenHolding:
    mint: str
    amount: float
    raw_amount: int = 0
    decimals: int = 0


class SolanaRpc:
    def __init__(self, http_url: str) -> None:
        self.http_url = http_url
        self._ssl = ssl.create_default_context(cafile=certifi.where())
        self._request_id = 0

    async def get_transaction(self, signature: str) -> dict[str, Any] | None:
        for delay in (0.25, 0.5, 1.0, 2.0):
            result = await asyncio.to_thread(self._get_transaction_sync, signature)
            if result is not None:
                return result
            await asyncio.sleep(delay)
        return None

    async def signatures_for_address(
        self, address: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        if not address or not 1 <= limit <= 1_000:
            raise ValueError("address history requires a valid limit")
        result = await asyncio.to_thread(
            self._request,
            "getSignaturesForAddress",
            [address, {"limit": limit, "commitment": "confirmed"}],
        )
        if not isinstance(result, list):
            raise ConnectionError("Solana signature history is unavailable")
        return [item for item in result if isinstance(item, dict)]

    async def token_holdings(self, owner: str) -> tuple[SolanaTokenHolding, ...]:
        responses = await asyncio.gather(
            *(
                asyncio.to_thread(
                    self._request,
                    "getTokenAccountsByOwner",
                    [
                        owner,
                        {"programId": program_id},
                        {"encoding": "jsonParsed", "commitment": "confirmed"},
                    ],
                )
                for program_id in TOKEN_PROGRAM_IDS
            )
        )
        totals: dict[str, Decimal] = {}
        raw_totals: dict[str, int] = {}
        decimals_by_mint: dict[str, int] = {}
        successful = False
        for response in responses:
            if not isinstance(response, dict):
                continue
            successful = True
            for item in response.get("value") or []:
                try:
                    info = item["account"]["data"]["parsed"]["info"]
                    mint = str(info["mint"])
                    token_amount = info["tokenAmount"]
                    amount = Decimal(str(token_amount["uiAmountString"]))
                    raw_value = token_amount.get("amount")
                    decimals_value = token_amount.get("decimals")
                    raw_amount = int(raw_value) if raw_value is not None else 0
                    decimals = (
                        int(decimals_value)
                        if decimals_value is not None
                        else 0
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if mint in IGNORED_MINTS or amount <= 0:
                    continue
                totals[mint] = totals.get(mint, Decimal(0)) + amount
                raw_totals[mint] = raw_totals.get(mint, 0) + raw_amount
                decimals_by_mint[mint] = decimals
        if not successful:
            raise ConnectionError("Solana token balances are unavailable")
        return tuple(
            SolanaTokenHolding(
                mint=mint,
                amount=float(amount),
                raw_amount=raw_totals[mint],
                decimals=decimals_by_mint[mint],
            )
            for mint, amount in sorted(totals.items())
        )

    async def token_balance(
        self, owner: str, mint: str
    ) -> SolanaTokenHolding:
        result = await asyncio.to_thread(
            self._request,
            "getTokenAccountsByOwner",
            [
                owner,
                {"mint": mint},
                {"encoding": "jsonParsed", "commitment": "confirmed"},
            ],
        )
        if not isinstance(result, dict):
            raise ConnectionError("Solana token balance is unavailable")
        raw_total = 0
        decimals: int | None = None
        for item in result.get("value") or []:
            try:
                token_amount = item["account"]["data"]["parsed"]["info"][
                    "tokenAmount"
                ]
                raw_total += int(token_amount["amount"])
                decimals = int(token_amount["decimals"])
            except (KeyError, TypeError, ValueError):
                continue
        if decimals is None:
            decimals = 6 if mint in IGNORED_MINTS else 0
        return SolanaTokenHolding(
            mint=mint,
            amount=float(
                Decimal(raw_total) / (Decimal(10) ** decimals)
            ),
            raw_amount=raw_total,
            decimals=decimals,
        )

    async def mint_decimals(self, mint: str) -> int:
        result = await asyncio.to_thread(
            self._request,
            "getTokenSupply",
            [mint, {"commitment": "confirmed"}],
        )
        if not isinstance(result, dict):
            raise ConnectionError("Solana token supply is unavailable")
        try:
            decimals = int(result["value"]["decimals"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConnectionError("Solana returned invalid mint decimals") from exc
        if decimals < 0:
            raise ConnectionError("Solana returned invalid mint decimals")
        return decimals

    async def simulate_transaction(
        self, signed_transaction_b64: str
    ) -> dict[str, Any]:
        """Simulate a signed transaction through RPC without broadcasting it."""
        result = await asyncio.to_thread(
            self._request,
            "simulateTransaction",
            [
                signed_transaction_b64,
                {
                    "encoding": "base64",
                    "sigVerify": True,
                    "replaceRecentBlockhash": False,
                    "commitment": "confirmed",
                },
            ],
        )
        if not isinstance(result, dict):
            raise ConnectionError("Solana transaction simulation is unavailable")
        value = result.get("value")
        if not isinstance(value, dict):
            raise ConnectionError("Solana returned an invalid simulation response")
        error = value.get("err")
        if error is not None:
            detail = json.dumps(error, sort_keys=True, separators=(",", ":"))
            raise TransactionSimulationFailed(f"Solana transaction simulation failed: {detail}")
        return value

    def _get_transaction_sync(self, signature: str) -> dict[str, Any] | None:
        result = self._request(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )
        return result if isinstance(result, dict) else None

    def _request(self, method: str, params: list[Any]) -> Any:
        self._request_id += 1
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params,
            }
        ).encode()
        request = urllib.request.Request(
            self.http_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "solana-launch-guard/0.2",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=15, context=self._ssl
            ) as response:
                payload = json.load(response)
        except (OSError, ValueError, urllib.error.URLError):
            return None
        if not isinstance(payload, dict) or payload.get("error"):
            return None
        return payload.get("result")


def _token_amount(item: dict[str, Any]) -> Decimal | None:
    """A balance, or None when the RPC entry cannot be read. Never 0 for an
    unreadable entry: a post-balance of 0 would read as selling everything."""
    ui = item.get("uiTokenAmount") or {}
    raw = ui.get("uiAmountString")
    if raw is not None:
        try:
            return Decimal(str(raw))
        except (ArithmeticError, ValueError):
            pass  # fall back to the raw integer amount below
    try:
        amount = Decimal(str(ui.get("amount") or 0))
        decimals = int(ui.get("decimals") or 0)
        return amount / (Decimal(10) ** decimals)
    except (ArithmeticError, ValueError, TypeError):
        return None


def parse_wallet_trades(
    transaction: dict[str, Any], wallet: str, signature: str, slot: int
) -> list[WalletTrade]:
    meta = transaction.get("meta") or {}
    message = ((transaction.get("transaction") or {}).get("message") or {})
    account_keys = message.get("accountKeys") or []
    normalized_keys = [
        str(item.get("pubkey")) if isinstance(item, dict) else str(item)
        for item in account_keys
    ]

    native_delta: float | None = None
    if wallet in normalized_keys:
        index = normalized_keys.index(wallet)
        pre_balances = meta.get("preBalances") or []
        post_balances = meta.get("postBalances") or []
        if index < len(pre_balances) and index < len(post_balances):
            native_delta = float(
                (Decimal(post_balances[index]) - Decimal(pre_balances[index]))
                / LAMPORTS_PER_SOL
            )

    before: dict[str, Decimal] = {}
    after: dict[str, Decimal] = {}
    unreadable: set[str] = set()
    for key, balances in (("preTokenBalances", before), ("postTokenBalances", after)):
        for item in meta.get(key) or []:
            if item.get("owner") != wallet:
                continue
            mint = str(item.get("mint"))
            amount = _token_amount(item)
            if amount is None:
                unreadable.add(mint)
                continue
            balances[mint] = balances.get(mint, Decimal(0)) + amount

    usdc_delta: float | None = (
        None if USDC_MINT in unreadable
        else float(after.get(USDC_MINT, Decimal(0)) - before.get(USDC_MINT, Decimal(0)))
    )

    trades: list[WalletTrade] = []
    for mint in sorted(set(before) | set(after)):
        if mint in IGNORED_MINTS or mint in unreadable:
            continue
        delta = after.get(mint, Decimal(0)) - before.get(mint, Decimal(0))
        if delta == 0:
            continue
        trades.append(
            WalletTrade(
                wallet=wallet,
                signature=signature,
                slot=slot,
                mint=mint,
                side="BUY" if delta > 0 else "SELL",
                token_delta=float(abs(delta)),
                native_sol_delta=native_delta,
                usdc_delta=usdc_delta,
            )
        )
    return trades


class WalletWatcher:
    def __init__(
        self,
        *,
        ws_url: str,
        rpc: SolanaRpc,
        wallets: tuple[str, ...],
        callback: Callable[[WalletTrade], Awaitable[None]],
    ) -> None:
        self.ws_url = ws_url
        self.rpc = rpc
        self.wallets = wallets
        self.callback = callback
        self._seen: set[tuple[str, str]] = set()
        self._ssl = ssl.create_default_context(cafile=certifi.where())

    async def run_forever(self) -> None:
        backoff = 1
        while True:
            try:
                LOGGER.info("Connecting to Solana RPC wallet feed")
                async with websockets.connect(
                    self.ws_url,
                    ssl=self._ssl,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=4_000_000,
                ) as websocket:
                    request_wallet: dict[int, str] = {}
                    subscription_wallet: dict[int, str] = {}
                    for request_id, wallet in enumerate(self.wallets, start=1):
                        request_wallet[request_id] = wallet
                        await websocket.send(
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": request_id,
                                    "method": "logsSubscribe",
                                    "params": [
                                        {"mentions": [wallet]},
                                        {"commitment": "confirmed"},
                                    ],
                                }
                            )
                        )

                    async for raw in websocket:
                        payload = json.loads(raw)
                        if "id" in payload and "result" in payload:
                            requested_wallet = request_wallet.get(
                                int(payload["id"])
                            )
                            if requested_wallet:
                                subscription_wallet[int(payload["result"])] = (
                                    requested_wallet
                                )
                                LOGGER.info(
                                    "Watching trader wallet %s", requested_wallet
                                )
                            continue

                        params = payload.get("params") or {}
                        result = params.get("result") or {}
                        context = result.get("context") or {}
                        value = result.get("value") or {}
                        subscription = int(params.get("subscription") or 0)
                        matched_wallet = subscription_wallet.get(subscription)
                        signature = str(value.get("signature") or "")
                        if (
                            not matched_wallet
                            or not signature
                            or value.get("err") is not None
                        ):
                            continue
                        key = (matched_wallet, signature)
                        if key in self._seen:
                            continue
                        self._seen.add(key)
                        if len(self._seen) > 10_000:
                            self._seen.clear()
                            self._seen.add(key)

                        transaction = await self.rpc.get_transaction(signature)
                        if transaction is None:
                            LOGGER.debug("Transaction unavailable: %s", signature)
                            continue
                        for trade in parse_wallet_trades(
                            transaction,
                            matched_wallet,
                            signature,
                            int(context.get("slot") or 0),
                        ):
                            await self.callback(trade)
                    backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning(
                    "Wallet feed disconnected (%s). Reconnecting in %d seconds",
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
