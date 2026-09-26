"""Read-only monitoring of CopyFomo's own trading wallet(s).

CopyFomo (a Telegram copy-trading bot) trades from its own separate
wallet(s), never Launch Guard's - confirmed 2026-09-23, correcting an
earlier, wrongly-premised fix (see live_trial.py's
COPYFOMO_PURCHASE_RANGES_USD, which assumed they shared a wallet). This
mixin only watches and logs CopyFomo's trades exactly as they happen, for
a later weekly performance report and a recommendation on whether to
change the leader traders it's copying - unlike the existing EVM-transfer/
wallet-copy handlers this reuses the watcher infra from, it never scores,
recommends, or acts on anything it sees.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .launch_guard_state import LaunchGuardState
from .multichain import EvmRpc, EvmTransfer, EvmWalletWatcher
from .wallet import SolanaRpc, WalletTrade, WalletWatcher

LOGGER = logging.getLogger("solana_launch_guard")


class CopyFomoMonitorMixin(LaunchGuardState):
    """Watches CopyFomo's own trading wallet(s); logs only, never acts."""

    async def handle_copyfomo_solana_trade(self, trade: WalletTrade) -> None:
        quote = await self.oracle.quote(trade.mint)
        symbol = quote.symbol if quote else trade.mint[:6]
        # The column is observed_price_sol; this used to store the USD price.
        price = quote.price_sol if quote else None
        inserted = self.store.save_wallet_trade(
            wallet=trade.wallet,
            signature=trade.signature,
            slot=trade.slot,
            mint=trade.mint,
            symbol=symbol,
            side=trade.side,
            token_delta=trade.token_delta,
            native_sol_delta=trade.native_sol_delta,
            observed_price_sol=price,
            usdc_delta=trade.usdc_delta,
        )
        if not inserted:
            return
        leader = dict(
            (address, name) for name, address in self.settings.copyfomo_leader_wallets
        ).get(trade.wallet)
        LOGGER.info(
            "%s %-4s chain=solana token=%s mint=%s amount=%.8g signature=%s",
            f"COPYFOMO-LEADER {leader}" if leader else "COPYFOMO",
            trade.side,
            symbol,
            trade.mint,
            trade.token_delta,
            trade.signature,
        )

    async def handle_copyfomo_evm_transfer(self, transfer: EvmTransfer) -> None:
        quote = await self.oracle.quote(transfer.contract, chain=transfer.chain)
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
            source="copyfomo_monitor",
        )
        if not inserted:
            return
        LOGGER.info(
            "COPYFOMO %-4s chain=%s token=%s amount=%.8g contract=%s tx=%s",
            transfer.direction,
            transfer.chain,
            transfer.symbol,
            transfer.token_amount,
            transfer.contract,
            transfer.transaction_hash,
        )

    def build_copyfomo_wallet_tasks(self) -> list[asyncio.Task[Any]]:
        tasks: list[asyncio.Task[Any]] = []
        solana_wallet = self.settings.copyfomo_solana_wallet
        if solana_wallet:
            # CopyFomo's own wallet plus its leaders' wallets, all read-only:
            # trades are recorded to wallet_trades and nothing is bought.
            leader_addresses = tuple(
                address for _, address in self.settings.copyfomo_leader_wallets
                if address != solana_wallet
            )
            solana_watcher = WalletWatcher(
                ws_url=self.settings.solana_rpc_ws_url,
                rpc=SolanaRpc(self.settings.solana_rpc_http_url),
                wallets=(solana_wallet, *leader_addresses),
                callback=self.handle_copyfomo_solana_trade,
            )
            tasks.append(asyncio.create_task(solana_watcher.run_forever()))
        else:
            LOGGER.warning(
                "No COPYFOMO_SOLANA_WALLET configured; CopyFomo Solana "
                "monitoring inactive"
            )

        evm_wallet = self.settings.copyfomo_evm_wallet
        if evm_wallet:
            chain = self.settings.copyfomo_evm_chain
            rpc_url = self.settings.evm_rpc_urls.get(chain, "")
            if not rpc_url:
                LOGGER.warning(
                    "COPYFOMO_EVM_WALLET is set but no RPC URL is configured "
                    "for COPYFOMO_EVM_CHAIN=%s; CopyFomo EVM monitoring "
                    "inactive",
                    chain,
                )
            else:
                evm_watcher = EvmWalletWatcher(
                    chain=chain,
                    rpc=EvmRpc(rpc_url),
                    wallet=evm_wallet,
                    callback=self.handle_copyfomo_evm_transfer,
                    poll_seconds=self.settings.copyfomo_evm_poll_seconds,
                )
                tasks.append(asyncio.create_task(evm_watcher.run_forever()))
        else:
            LOGGER.warning(
                "No COPYFOMO_EVM_WALLET configured; CopyFomo EVM monitoring "
                "inactive"
            )
        return tasks
