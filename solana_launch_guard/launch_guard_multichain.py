from __future__ import annotations

import logging
import asyncio
from typing import Any

from .launch_guard_state import LaunchGuardState
from .market import MarketQuote
from .multichain import (
    EvmRpc,
    EvmTransfer,
    EvmWalletWatcher,
    HyperCoreFill,
    HyperCoreState,
    HyperCoreWatcher,
)
from .launch_guard_support import _is_stock_token_symbol

LOGGER = logging.getLogger("solana_launch_guard")


class MultichainMixin(LaunchGuardState):
    """Multichain (EVM/HyperCore/Robinhood) wallet feeds."""

    async def run_multichain_feed(self, chains: tuple[str, ...]) -> None:
        LOGGER.info(
            "Multichain paper-recommendation feed active: %s "
            "(no automatic orders)",
            ", ".join(chains),
        )
        while True:
            discovery_results = await asyncio.gather(
                *(self.oracle.discover_token_profiles(chain) for chain in chains)
            )
            stock_tokens = await self.oracle.robinhood_stock_token_addresses()
            stock_symbols = await self.oracle.robinhood_stock_token_symbols()
            if (
                stock_tokens is None or stock_symbols is None
            ) and "robinhood" in chains:
                LOGGER.warning(
                    "Robinhood Stock Token registry unavailable; "
                    "skipping Robinhood candidates this pass"
                )

            work: list[tuple[str, str]] = []
            configured = self.settings.multichain_token_addresses
            for chain, discovered in zip(chains, discovery_results, strict=True):
                if chain == "robinhood" and stock_tokens is None:
                    continue
                addresses: dict[str, str] = {
                    address.casefold(): address
                    for address in configured.get(chain, ())
                }
                for address in discovered:
                    addresses.setdefault(address.casefold(), address)
                for key, address in addresses.items():
                    if chain == "robinhood" and key in (stock_tokens or ()):
                        continue
                    work.append((chain, address))

            self.multichain_pending_count = len(work)
            semaphore = asyncio.Semaphore(5)

            async def fetch(
                chain: str,
                address: str,
                limiter: asyncio.Semaphore = semaphore,
            ) -> tuple[str, str, MarketQuote | None]:
                async with limiter:
                    quote = await self.oracle.quote(address, chain=chain)
                return chain, address, quote

            try:
                quotes = await asyncio.gather(
                    *(fetch(chain, address) for chain, address in work)
                )
                for chain, address, quote in quotes:
                    if (
                        quote is not None
                        and stock_symbols is not None
                        and _is_stock_token_symbol(
                            quote.symbol, stock_symbols
                        )
                    ):
                        LOGGER.info(
                            "%s REJECT %-10s contract=%s "
                            "reason=tokenized stock symbol",
                            chain.upper(),
                            quote.symbol,
                            address,
                        )
                        continue
                    result = self.intelligence.score(quote)
                    symbol = quote.symbol if quote else address[:10]
                    result_key = f"{chain}:{address.casefold()}"
                    current_result = (result.tier, result.total_score)
                    previous_result = self.multichain_last_result.get(result_key)
                    if current_result != previous_result:
                        LOGGER.info(
                            "%s %-9s %-10s score=%d contract=%s %s",
                            chain.upper(),
                            result.tier,
                            symbol,
                            result.total_score,
                            address,
                            "; ".join(result.reasons),
                        )
                        self.multichain_last_result[result_key] = current_result

                    if quote is None or not result.accepted:
                        continue
                    self.store.save_intelligence_score(
                        mint=address,
                        symbol=symbol,
                        tier=result.tier,
                        total_score=result.total_score,
                        safety_score=result.safety_score,
                        momentum_score=result.momentum_score,
                        reasons=result.reasons,
                    )
                    self.recommendations.add(quote, result)
            finally:
                self.multichain_pending_count = 0

            await asyncio.sleep(self.settings.multichain_poll_seconds)

    async def handle_evm_transfer(self, transfer: EvmTransfer) -> None:
        quote = await self.oracle.quote(
            transfer.contract, chain=transfer.chain
        )
        price_usd = quote.price_usd if quote else None
        inserted = self.store.save_wallet_event(
            chain=transfer.chain,
            wallet=transfer.wallet,
            event_id=transfer.event_id,
            block_number=transfer.block_number,
            token_address=transfer.contract,
            symbol=transfer.symbol,
            direction=transfer.direction,
            token_amount=transfer.token_amount,
            price_usd=price_usd,
            source="EVM_TRANSFER",
        )
        if not inserted:
            return
        LOGGER.info(
            "WALLET %-4s chain=%s token=%s amount=%.8g contract=%s tx=%s",
            transfer.direction,
            transfer.chain,
            transfer.symbol,
            transfer.token_amount,
            transfer.contract,
            transfer.transaction_hash,
        )
        if quote is not None:
            # run_multichain_feed excludes tokenized stocks from becoming
            # trading candidates (pullback/momentum sniping logic doesn't
            # fit a real stock's price action); a wallet transfer of one -
            # ordinary brokerage activity reflected on-chain, not a launch
            # to snipe - must not bypass that exclusion. Checked for every
            # chain, not just "robinhood": tokenized-stock symbols (e.g.
            # Backed Finance's xStocks) also show up on other chains, and
            # launch_guard_ingestion.py's equivalent check is chain-agnostic
            # too, so this must not be narrower than that.
            stock_symbols = await self.oracle.robinhood_stock_token_symbols()
            if _is_stock_token_symbol(quote.symbol, stock_symbols):
                LOGGER.info(
                    "%s REJECT %-10s contract=%s reason=tokenized stock symbol",
                    transfer.chain.upper(),
                    quote.symbol,
                    transfer.contract,
                )
            else:
                result = self.intelligence.score(quote)
                if result.accepted:
                    self.recommendations.add(quote, result)

    async def handle_hypercore_fill(self, fill: HyperCoreFill) -> None:
        direction = "BUY" if fill.side.upper() == "B" else "SELL"
        inserted = self.store.save_wallet_event(
            chain="hypercore",
            wallet=fill.wallet,
            event_id=fill.fill_id,
            block_number=None,
            token_address=fill.coin,
            symbol=fill.coin,
            direction=direction,
            token_amount=fill.size,
            price_usd=fill.price,
            source="HYPERCORE_FILL",
        )
        if inserted:
            LOGGER.info(
                "HYPERCORE %-4s coin=%s size=%.8g price=$%.8g fill=%s",
                direction,
                fill.coin,
                fill.size,
                fill.price,
                fill.fill_id,
            )

    async def handle_hypercore_state(self, state: HyperCoreState) -> None:
        spot = ", ".join(
            f"{coin}={amount:.8g}" for coin, amount in state.spot_balances
        ) or "none"
        perps = ", ".join(
            f"{coin}={size:+.8g}" for coin, size in state.perp_positions
        ) or "none"
        LOGGER.info("HYPERCORE HOLDINGS spot=[%s] perps=[%s]", spot, perps)

    def build_multichain_wallet_tasks(self) -> list[asyncio.Task[Any]]:
        tasks: list[asyncio.Task[Any]] = []
        wallet = self.settings.evm_wallet_address
        if wallet:
            for chain, rpc_url in self.settings.evm_rpc_urls.items():
                if not rpc_url:
                    LOGGER.warning(
                        "%s wallet monitoring inactive: RPC URL is empty", chain
                    )
                    continue
                watcher = EvmWalletWatcher(
                    chain=chain,
                    rpc=EvmRpc(rpc_url),
                    wallet=wallet,
                    callback=self.handle_evm_transfer,
                    poll_seconds=self.settings.evm_wallet_poll_seconds,
                )
                tasks.append(asyncio.create_task(watcher.run_forever()))
        else:
            LOGGER.warning(
                "No EVM_WALLET_ADDRESS configured; EVM wallet monitoring inactive"
            )

        hyperliquid = self.settings.hyperliquid_address
        if hyperliquid:
            hypercore_watcher = HyperCoreWatcher(
                wallet=hyperliquid,
                fill_callback=self.handle_hypercore_fill,
                state_callback=self.handle_hypercore_state,
                poll_seconds=self.settings.evm_wallet_poll_seconds,
            )
            tasks.append(asyncio.create_task(hypercore_watcher.run_forever()))
        else:
            LOGGER.warning(
                "No HYPERLIQUID_ADDRESS configured; HyperCore monitoring inactive"
            )
        return tasks
