from __future__ import annotations

import asyncio
import json
import logging
import ssl
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import certifi

LOGGER = logging.getLogger("solana_launch_guard")
TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)


def _address_topic(address: str) -> str:
    return "0x" + address.removeprefix("0x").lower().rjust(64, "0")


def _decode_abi_string(value: str) -> str | None:
    try:
        raw = bytes.fromhex(value.removeprefix("0x"))
    except ValueError:
        return None
    if not raw:
        return None
    try:
        if len(raw) >= 64 and int.from_bytes(raw[:32], "big") == 32:
            length = int.from_bytes(raw[32:64], "big")
            return raw[64 : 64 + length].decode("utf-8", errors="replace")
        return raw.rstrip(b"\x00").decode("utf-8", errors="replace")
    except (UnicodeDecodeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class EvmTransfer:
    chain: str
    wallet: str
    transaction_hash: str
    block_number: int
    log_index: int
    contract: str
    symbol: str
    direction: str
    token_amount: float

    @property
    def event_id(self) -> str:
        return f"{self.transaction_hash}:{self.log_index}"


class EvmRpc:
    def __init__(self, url: str) -> None:
        self.url = url
        self._ssl = ssl.create_default_context(cafile=certifi.where())
        self._request_id = 0
        self._metadata: dict[str, tuple[str, int]] = {}

    async def block_number(self) -> int | None:
        result = await asyncio.to_thread(self._request, "eth_blockNumber", [])
        try:
            return int(str(result), 16)
        except (TypeError, ValueError):
            return None

    async def transfers(
        self,
        *,
        chain: str,
        wallet: str,
        from_block: int,
        to_block: int,
    ) -> list[EvmTransfer]:
        wallet_topic = _address_topic(wallet)
        filters = (
            ("IN", [TRANSFER_TOPIC, None, wallet_topic]),
            ("OUT", [TRANSFER_TOPIC, wallet_topic]),
        )
        raw_logs: list[tuple[str, dict[str, Any]]] = []
        for direction, topics in filters:
            result = await asyncio.to_thread(
                self._request,
                "eth_getLogs",
                [
                    {
                        "fromBlock": hex(from_block),
                        "toBlock": hex(to_block),
                        "topics": topics,
                    }
                ],
            )
            if result is None:
                raise ConnectionError(
                    f"{chain} eth_getLogs failed for blocks "
                    f"{from_block}-{to_block}"
                )
            if isinstance(result, list):
                raw_logs.extend(
                    (direction, item)
                    for item in result
                    if isinstance(item, dict)
                )

        transfers: list[EvmTransfer] = []
        seen: set[tuple[str, int, str]] = set()
        for direction, item in raw_logs:
            tx_hash = str(item.get("transactionHash") or "")
            contract = str(item.get("address") or "")
            try:
                log_index = int(str(item.get("logIndex") or "0x0"), 16)
                block = int(str(item.get("blockNumber") or "0x0"), 16)
                raw_amount = int(str(item.get("data") or "0x0"), 16)
            except ValueError:
                continue
            key = (tx_hash, log_index, direction)
            if not tx_hash or not contract or key in seen:
                continue
            seen.add(key)
            symbol, decimals = await self.token_metadata(contract)
            amount = raw_amount / (10**decimals)
            transfers.append(
                EvmTransfer(
                    chain=chain,
                    wallet=wallet,
                    transaction_hash=tx_hash,
                    block_number=block,
                    log_index=log_index,
                    contract=contract,
                    symbol=symbol,
                    direction=direction,
                    token_amount=amount,
                )
            )
        return sorted(
            transfers, key=lambda item: (item.block_number, item.log_index)
        )

    async def token_metadata(self, contract: str) -> tuple[str, int]:
        key = contract.casefold()
        cached = self._metadata.get(key)
        if cached is not None:
            return cached
        symbol_result, decimals_result = await asyncio.gather(
            asyncio.to_thread(
                self._request,
                "eth_call",
                [{"to": contract, "data": "0x95d89b41"}, "latest"],
            ),
            asyncio.to_thread(
                self._request,
                "eth_call",
                [{"to": contract, "data": "0x313ce567"}, "latest"],
            ),
        )
        symbol = (
            _decode_abi_string(symbol_result)
            if isinstance(symbol_result, str)
            else None
        )
        try:
            decimals = int(str(decimals_result), 16)
        except (TypeError, ValueError):
            decimals = 18
        metadata = (symbol or contract[:10], min(max(decimals, 0), 36))
        self._metadata[key] = metadata
        return metadata

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
            self.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "solana-launch-guard/0.7",
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


class EvmWalletWatcher:
    def __init__(
        self,
        *,
        chain: str,
        rpc: EvmRpc,
        wallet: str,
        callback: Callable[[EvmTransfer], Awaitable[None]],
        poll_seconds: float = 10,
        lookback_blocks: int = 250,
    ) -> None:
        self.chain = chain
        self.rpc = rpc
        self.wallet = wallet
        self.callback = callback
        self.poll_seconds = poll_seconds
        self.lookback_blocks = lookback_blocks

    async def run_forever(self) -> None:
        next_block: int | None = None
        backoff = 1
        while True:
            try:
                latest = await self.rpc.block_number()
                if latest is None:
                    raise ConnectionError("latest block unavailable")
                if next_block is None:
                    next_block = max(0, latest - self.lookback_blocks)
                    LOGGER.info(
                        "Watching %s wallet %s from block %d",
                        self.chain,
                        self.wallet,
                        next_block,
                    )
                while next_block <= latest:
                    end_block = min(latest, next_block + 499)
                    transfers = await self.rpc.transfers(
                        chain=self.chain,
                        wallet=self.wallet,
                        from_block=next_block,
                        to_block=end_block,
                    )
                    for transfer in transfers:
                        await self.callback(transfer)
                    next_block = end_block + 1
                backoff = 1
                await asyncio.sleep(self.poll_seconds)
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError, ValueError) as exc:
                LOGGER.warning(
                    "%s wallet feed unavailable (%s); retrying in %ds",
                    self.chain,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


@dataclass(frozen=True, slots=True)
class HyperCoreFill:
    wallet: str
    fill_id: str
    coin: str
    side: str
    size: float
    price: float
    timestamp_ms: int


@dataclass(frozen=True, slots=True)
class HyperCoreState:
    wallet: str
    spot_balances: tuple[tuple[str, float], ...]
    perp_positions: tuple[tuple[str, float], ...]


class HyperCoreWatcher:
    def __init__(
        self,
        *,
        wallet: str,
        fill_callback: Callable[[HyperCoreFill], Awaitable[None]],
        state_callback: Callable[[HyperCoreState], Awaitable[None]],
        poll_seconds: float = 10,
    ) -> None:
        self.wallet = wallet
        self.fill_callback = fill_callback
        self.state_callback = state_callback
        self.poll_seconds = poll_seconds
        self._ssl = ssl.create_default_context(cafile=certifi.where())
        self._seen_fills: set[str] = set()
        self._last_state: HyperCoreState | None = None

    async def run_forever(self) -> None:
        backoff = 1
        while True:
            try:
                spot, perps, fills = await asyncio.gather(
                    asyncio.to_thread(
                        self._info,
                        {"type": "spotClearinghouseState", "user": self.wallet},
                    ),
                    asyncio.to_thread(
                        self._info,
                        {"type": "clearinghouseState", "user": self.wallet},
                    ),
                    asyncio.to_thread(
                        self._info,
                        {
                            "type": "userFills",
                            "user": self.wallet,
                            "aggregateByTime": True,
                        },
                    ),
                )
                if spot is None or perps is None or fills is None:
                    raise ConnectionError("HyperCore public API unavailable")
                state = self._parse_state(spot, perps)
                if state != self._last_state:
                    await self.state_callback(state)
                    self._last_state = state
                parsed_fills = self._parse_fills(fills)
                for fill in reversed(parsed_fills[:100]):
                    if fill.fill_id in self._seen_fills:
                        continue
                    self._seen_fills.add(fill.fill_id)
                    await self.fill_callback(fill)
                if len(self._seen_fills) > 10_000:
                    self._seen_fills = {item.fill_id for item in parsed_fills[:100]}
                backoff = 1
                await asyncio.sleep(self.poll_seconds)
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError, ValueError) as exc:
                LOGGER.warning(
                    "HyperCore feed unavailable (%s); retrying in %ds",
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _info(self, payload: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            "https://api.hyperliquid.xyz/info",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "solana-launch-guard/0.7",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=15, context=self._ssl
            ) as response:
                return json.load(response)
        except (OSError, ValueError, urllib.error.URLError):
            return None

    def _parse_state(self, spot: Any, perps: Any) -> HyperCoreState:
        balances: list[tuple[str, float]] = []
        if isinstance(spot, dict):
            for item in spot.get("balances") or []:
                if not isinstance(item, dict):
                    continue
                total = float(item.get("total") or 0)
                if total:
                    balances.append((str(item.get("coin") or "?"), total))
        positions: list[tuple[str, float]] = []
        if isinstance(perps, dict):
            for item in perps.get("assetPositions") or []:
                position = item.get("position") if isinstance(item, dict) else None
                if not isinstance(position, dict):
                    continue
                size = float(position.get("szi") or 0)
                if size:
                    positions.append((str(position.get("coin") or "?"), size))
        return HyperCoreState(
            wallet=self.wallet,
            spot_balances=tuple(sorted(balances)),
            perp_positions=tuple(sorted(positions)),
        )

    def _parse_fills(self, payload: Any) -> list[HyperCoreFill]:
        if not isinstance(payload, list):
            return []
        result: list[HyperCoreFill] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                timestamp = int(item.get("time") or 0)
                coin = str(item.get("coin") or "?")
                side = str(item.get("side") or "?")
                size = float(item.get("sz") or 0)
                price = float(item.get("px") or 0)
            except (TypeError, ValueError):
                continue
            fill_id = str(
                item.get("tid")
                or item.get("hash")
                or f"{timestamp}:{coin}:{side}:{size}:{price}"
            )
            result.append(
                HyperCoreFill(
                    wallet=self.wallet,
                    fill_id=fill_id,
                    coin=coin,
                    side=side,
                    size=size,
                    price=price,
                    timestamp_ms=timestamp,
                )
            )
        return sorted(result, key=lambda item: item.timestamp_ms, reverse=True)
