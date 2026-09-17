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
IGNORED_MINTS = {
    "So11111111111111111111111111111111111111112",  # wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYDg1QhVg2u4NfrJkGg7QZp",   # USDT
}


@dataclass(frozen=True, slots=True)
class WalletTrade:
    wallet: str
    signature: str
    slot: int
    mint: str
    side: str
    token_delta: float
    native_sol_delta: float | None


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

    def _get_transaction_sync(self, signature: str) -> dict[str, Any] | None:
        self._request_id += 1
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": "getTransaction",
                "params": [
                    signature,
                    {
                        "encoding": "jsonParsed",
                        "commitment": "confirmed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
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
        return payload.get("result")


def _token_amount(item: dict[str, Any]) -> Decimal:
    ui = item.get("uiTokenAmount") or {}
    raw = ui.get("uiAmountString")
    if raw is not None:
        try:
            return Decimal(str(raw))
        except Exception:
            return Decimal(0)
    amount = Decimal(str(ui.get("amount") or 0))
    decimals = int(ui.get("decimals") or 0)
    return amount / (Decimal(10) ** decimals)


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
    for item in meta.get("preTokenBalances") or []:
        if item.get("owner") == wallet:
            before[str(item.get("mint"))] = (
                before.get(str(item.get("mint")), Decimal(0)) + _token_amount(item)
            )
    for item in meta.get("postTokenBalances") or []:
        if item.get("owner") == wallet:
            after[str(item.get("mint"))] = (
                after.get(str(item.get("mint")), Decimal(0)) + _token_amount(item)
            )

    trades: list[WalletTrade] = []
    for mint in sorted(set(before) | set(after)):
        if mint in IGNORED_MINTS:
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
                            wallet = request_wallet.get(int(payload["id"]))
                            if wallet:
                                subscription_wallet[int(payload["result"])] = wallet
                                LOGGER.info("Watching trader wallet %s", wallet)
                            continue

                        params = payload.get("params") or {}
                        result = params.get("result") or {}
                        context = result.get("context") or {}
                        value = result.get("value") or {}
                        subscription = int(params.get("subscription") or 0)
                        wallet = subscription_wallet.get(subscription)
                        signature = str(value.get("signature") or "")
                        if not wallet or not signature or value.get("err") is not None:
                            continue
                        key = (wallet, signature)
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
                            wallet,
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
