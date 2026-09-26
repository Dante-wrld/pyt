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

# A leader selling at least this share of their holding is worth recording.
LEADER_SELL_WARNING_FRACTION = 0.25


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
        if leader and trade.side == "SELL":
            self._record_leader_sell(leader, trade, symbol)
        LOGGER.info(
            "%s %-4s chain=solana token=%s mint=%s amount=%.8g signature=%s",
            f"COPYFOMO-LEADER {leader}" if leader else "COPYFOMO",
            trade.side,
            symbol,
            trade.mint,
            trade.token_delta,
            trade.signature,
        )

    def _record_leader_sell(self, leader: str, trade: WalletTrade, symbol: str) -> None:
        """Record-only exit warning: a leader sold a large part of a token
        that is on our board or that we hold. Nothing is sold because of it;
        it is stored as a LEADER_SELL event so it can be tested as an exit
        rule later."""
        held = self.leader_holdings.get(trade.mint, {})
        before = held.get(leader)
        fraction = trade.token_delta / before if before else None
        if before is not None:
            held[leader] = max(0.0, before - trade.token_delta)
        on_board = f"solana:{trade.mint}" in self.recommendations.candidates
        owned = self.store.has_open_auto_buy_position(
            "solana", trade.mint
        ) or self.broker.has_position(trade.mint)
        if not (on_board or owned):
            return
        if fraction is not None and fraction < LEADER_SELL_WARNING_FRACTION:
            return
        self.store.save_event("LEADER_SELL", {
            "leader": leader, "mint": trade.mint, "symbol": symbol,
            "tokens_sold": trade.token_delta, "fraction_of_holding": fraction,
            "usdc_received": trade.usdc_delta, "on_board": on_board,
            "held_by_us": owned, "signature": trade.signature,
        }, trade.mint)
        LOGGER.warning(
            "LEADER SELL WARNING %s sold %s of %s%s%s (record only)",
            leader,
            f"{fraction:.0%}" if fraction is not None else "an unknown share",
            symbol,
            " - we hold it" if owned else "",
            " - on the board" if on_board else "",
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
        tasks.extend(self._leader_evm_tasks(exclude=evm_wallet))
        return tasks

    def _leader_evm_tasks(self, *, exclude: str | None) -> list[asyncio.Task[Any]]:
        """Record the leaders' EVM transfers read-only (wallet_events), so
        the history exists if CopyFomo starts copying them on that chain."""
        leaders = [
            (name, address)
            for name, address in self.settings.copyfomo_leader_evm_wallets
            if address.lower() != (exclude or "").lower()
        ]
        if not leaders:
            return []
        chain = self.settings.copyfomo_evm_chain
        rpc_url = self.settings.evm_rpc_urls.get(chain, "")
        if not rpc_url:
            LOGGER.warning(
                "COPYFOMO_LEADER_EVM_WALLETS is set but no RPC URL is configured "
                "for COPYFOMO_EVM_CHAIN=%s; leader EVM recording inactive",
                chain,
            )
            return []
        rpc = EvmRpc(rpc_url)
        return [
            asyncio.create_task(EvmWalletWatcher(
                chain=chain,
                rpc=rpc,
                wallet=address,
                callback=self.handle_copyfomo_evm_transfer,
                poll_seconds=self.settings.copyfomo_evm_poll_seconds,
            ).run_forever())
            for _, address in leaders
        ]
