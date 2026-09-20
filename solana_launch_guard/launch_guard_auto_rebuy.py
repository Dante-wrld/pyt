from __future__ import annotations

import logging
import asyncio
import time
from collections.abc import Mapping
from typing import Any

from .execution import (
    BuyIntent,
    USDC_MINT,
)
from .launch_guard_state import LaunchGuardState
from .market import MarketQuote
from .portfolio import OwnedHolding
from .rebuy_assessment import auto_rebuy_recovery_assessment
from .wallet import (
    SolanaRpc,
    SolanaTokenHolding,
)

LOGGER = logging.getLogger("solana_launch_guard")


class AutoRebuyMixin(LaunchGuardState):
    """Deciding whether to re-enter a position after a stop-out."""

    async def _monitor_auto_rebuys(
        self,
        balances_by_mint: Mapping[str, SolanaTokenHolding],
        rpc: SolanaRpc,
    ) -> None:
        watches = self.store.active_auto_rebuy_watches()
        if not watches:
            return
        semaphore = asyncio.Semaphore(5)

        async def load_quote(
            watch: Mapping[str, Any],
        ) -> tuple[Mapping[str, Any], MarketQuote | None]:
            async with semaphore:
                quote = await self.oracle.quote(
                    str(watch["token_address"]), chain=str(watch["chain"])
                )
            return watch, quote

        observations = await asyncio.gather(
            *(load_quote(watch) for watch in watches)
        )
        observed_at = time.time()
        for watch, quote in observations:
            mint = str(watch["token_address"])
            symbol = str(watch["symbol"])
            if mint in self.settings.auto_buy_excluded_mints:
                self.store.expire_auto_rebuy_watch(
                    token_address=mint,
                    reason="mint is excluded from automatic buys",
                )
                continue
            wallet_balance = balances_by_mint.get(mint)
            if wallet_balance is not None and wallet_balance.raw_amount > 0:
                self.store.expire_auto_rebuy_watch(
                    token_address=mint,
                    reason=(
                        "wallet already holds this mint; cost-basis mixing "
                        "blocked"
                    ),
                )
                continue
            qualifying, reason, metrics = auto_rebuy_recovery_assessment(
                watch,
                quote,
                self.settings,
                now=observed_at,
            )
            age = observed_at - float(watch["sold_at_epoch"])
            if age > self.settings.auto_rebuy_max_watch_seconds:
                self.store.expire_auto_rebuy_watch(
                    token_address=mint,
                    reason="recovery watch expired",
                )
                continue
            if quote is None or quote.price_usd is None or quote.price_usd <= 0:
                self.store.reset_auto_rebuy_confirmation(
                    token_address=mint,
                    reason=reason,
                )
                continue
            updated = self.store.record_auto_rebuy_observation(
                token_address=mint,
                current_price_usd=quote.price_usd,
                observed_at_epoch=observed_at,
                qualifying=qualifying,
                confirmation_required=(
                    self.settings.auto_rebuy_confirmation_polls
                ),
                reason=reason,
            )
            if qualifying:
                LOGGER.warning(
                    "AUTO-REBUY CONFIRMING %s polls=%d/%d drop=%.1f%% "
                    "rebound=%.1f%%",
                    symbol,
                    int(updated["confirmation_count"]),
                    self.settings.auto_rebuy_confirmation_polls,
                    metrics["drop_pct"],
                    metrics["rebound_pct"],
                )
            if updated["status"] == "READY":
                await self._execute_auto_rebuy(updated, quote, rpc)

    async def _execute_auto_rebuy(
        self,
        watch: Mapping[str, Any],
        quote: MarketQuote,
        rpc: SolanaRpc,
    ) -> None:
        async with self.auto_buy_lock:
            await self._execute_auto_rebuy_locked(watch, quote, rpc)

    async def _execute_auto_rebuy_locked(
        self,
        watch: Mapping[str, Any],
        quote: MarketQuote,
        rpc: SolanaRpc,
    ) -> None:
        mint = str(watch["token_address"])
        symbol = str(watch["symbol"])
        seed_raw = round(
            min(
                self.settings.auto_buy_seed_size_usdc,
                self.settings.auto_rebuy_max_size_usdc,
            )
            * 1_000_000
        )
        try:
            amount_raw, funding_source = self.store.preview_auto_buy_budget(
                seed_size_usdc_raw=seed_raw,
                max_seed_buys=self.settings.auto_buy_max_seed_buys,
                max_open_positions=(
                    self.settings.auto_buy_max_open_positions
                ),
            )
        except ValueError as exc:
            LOGGER.info("AUTO-REBUY WAITING %s (%s)", symbol, exc)
            return
        event_key = f"solana:{mint}:auto-rebuy:{int(watch['cycle'])}"
        intent = BuyIntent(
            mint=mint,
            symbol=symbol,
            event_key=event_key,
            amount_usdc_raw=amount_raw,
            funding_source=funding_source,
        )
        if self.auto_buyer is None:
            if event_key not in self.auto_buy_dry_run_seen:
                LOGGER.warning(
                    "AUTO-REBUY READY (DRY RUN) %s amount=$%.2f "
                    "funding=%s reason=%s",
                    symbol,
                    amount_raw / 1_000_000,
                    funding_source,
                    watch["last_reason"],
                )
                self.auto_buy_dry_run_seen.add(event_key)
            return

        assert self.settings.solana_wallet_address is not None
        try:
            usdc = await rpc.token_balance(
                self.settings.solana_wallet_address, USDC_MINT
            )
            existing = await rpc.token_balance(
                self.settings.solana_wallet_address, mint
            )
            output_decimals = await rpc.mint_decimals(mint)
            if usdc.raw_amount < amount_raw:
                raise ValueError("wallet USDC balance is below the buy amount")
            if existing.raw_amount > 0:
                raise ValueError(
                    "wallet already holds this mint; cost-basis mixing blocked"
                )
            simulation = await self.auto_buyer.preflight(intent, rpc)
            prepared = simulation.prepared
        except (ConnectionError, RuntimeError, ValueError) as exc:
            LOGGER.warning("AUTO-REBUY NOT SUBMITTED %s (%s)", symbol, exc)
            return
        claimed = self.store.begin_auto_buy_execution(
            event_key=event_key,
            token_address=mint,
            symbol=symbol,
            funding_source=funding_source,
            input_usdc_raw=prepared.input_amount_raw,
            expected_output_raw=prepared.expected_output_raw,
        )
        if not claimed:
            return
        try:
            receipt = await self.auto_buyer.execute(prepared)
            if receipt.output_amount_raw <= 0:
                raise RuntimeError("confirmed re-buy reported no token output")
        except (ConnectionError, RuntimeError, ValueError) as exc:
            failure_signature = getattr(exc, "signature", None)
            self.store.freeze_auto_buy_execution(
                event_key=event_key,
                error=str(exc),
                signature=failure_signature,
            )
            review_reason = str(exc) + (
                f"; signature={failure_signature}"
                if failure_signature
                else ""
            )
            self.store.freeze_auto_rebuy(
                token_address=mint, error=review_reason
            )
            LOGGER.error(
                "AUTO-REBUY FROZEN FOR REVIEW %s (%s)", symbol, exc
            )
            if self.push_client is not None:
                try:
                    await self.push_client.send(
                        title="🔴 Launch Guard: RE-BUY NEEDS REVIEW",
                        message=(
                            f"TOKEN: {symbol} • SOLANA\n"
                            f"No automatic retry will occur.\nReason: {exc}"
                            + (
                                f"\nSignature: {failure_signature}"
                                if failure_signature
                                else ""
                            )
                        ),
                        sound="siren",
                        priority=1,
                    )
                except ConnectionError as notification_exc:
                    LOGGER.warning(
                        "Could not send auto-rebuy review alert (%s)",
                        notification_exc,
                    )
            return

        self.store.complete_auto_buy_execution(
            event_key=event_key,
            signature=receipt.signature,
            actual_output_raw=receipt.output_amount_raw,
            output_decimals=output_decimals,
        )
        quantity = receipt.output_amount_raw / (10**output_decimals)
        cost_usdc = receipt.input_amount_raw / 1_000_000
        self.store.save_owned_holding(
            OwnedHolding(
                chain="solana",
                token_address=mint,
                symbol=symbol,
                quantity=quantity,
                entry_price=cost_usdc / quantity,
                price_currency="USD",
                cost_amount=cost_usdc,
            )
        )
        self.store.complete_auto_rebuy(
            token_address=mint,
            buy_signature=receipt.signature,
            reset_auto_sell=True,
        )
        self.store.clear_auto_sell_signal_confirmation(mint)
        self.portfolio_advisor.restore_state(
            chain="solana",
            token_address=mint,
            peak_price=quote.price_usd or cost_usdc / quantity,
            baseline_liquidity_usd=quote.liquidity_usd or 0.0,
        )
        self.store.save_portfolio_state(
            chain="solana",
            token_address=mint,
            peak_price=quote.price_usd or cost_usdc / quantity,
            baseline_liquidity_usd=quote.liquidity_usd or 0.0,
        )
        LOGGER.warning(
            "AUTO-REBUY CONFIRMED %s amount=$%.2f cycle=%d signature=%s",
            symbol,
            cost_usdc,
            int(watch["cycle"]),
            receipt.signature,
        )
        if self.push_client is not None:
            try:
                await self.push_client.send(
                    title="🟢 Launch Guard: RE-BUY CONFIRMED",
                    message=(
                        f"TOKEN: {symbol} • SOLANA\n"
                        f"Amount: ${cost_usdc:.2f}\n"
                        f"Cycle: {int(watch['cycle'])}\n"
                        f"Signature: {receipt.signature}"
                    ),
                    url=f"https://solscan.io/tx/{receipt.signature}",
                    url_title="Open Solscan",
                    sound="cashregister",
                    priority=1,
                )
            except ConnectionError as notification_exc:
                LOGGER.warning(
                    "Re-buy confirmed, but the phone alert failed (%s)",
                    notification_exc,
                )
