from __future__ import annotations

import logging

from .execution import (
    BuyIntent,
    USDC_MINT,
)
from .launch_guard_state import LaunchGuardState
from .portfolio import OwnedHolding
from .recommendations import RecommendationCandidate
from .wallet import SolanaRpc
from .launch_guard_support import auto_buy_discovery_rejection

LOGGER = logging.getLogger("solana_launch_guard")


class AutoBuyMixin(LaunchGuardState):
    """Deciding whether to open a new live position from a recommendation candidate."""

    async def _maybe_auto_buy(
        self, candidate: RecommendationCandidate
    ) -> None:
        async with self.auto_buy_lock:
            await self._maybe_auto_buy_locked(candidate)

    async def _maybe_auto_buy_locked(
        self, candidate: RecommendationCandidate
    ) -> None:
        if (
            not self.settings.auto_buy_enabled
            or candidate.chain != "solana"
            or candidate.decision not in {"BUY NOW", "BUY ZONE", "MOMENTUM BUY"}
            or candidate.mint in self.settings.auto_buy_excluded_mints
        ):
            return
        if self.settings.auto_buy_discovery:
            rejection = auto_buy_discovery_rejection(
                candidate, self.settings
            )
            if rejection is not None:
                return
        policy = self.store.load_auto_buy_policy(candidate.mint)
        if policy is None:
            if not self.settings.auto_buy_discovery:
                return
            try:
                self.store.arm_auto_buy(candidate.mint, candidate.symbol)
            except ValueError as exc:
                LOGGER.info(
                    "AUTO-BUY DISCOVERY SKIPPED %s (%s)",
                    candidate.symbol,
                    exc,
                )
                return
            policy = self.store.load_auto_buy_policy(candidate.mint)
            LOGGER.warning(
                "AUTO-BUY DISCOVERED %s mint=%s score=%d liquidity=$%.0f",
                candidate.symbol,
                candidate.mint,
                candidate.signal_score,
                candidate.liquidity_usd or 0,
            )
        if policy is None or not bool(policy["armed"]):
            return
        if self.settings.auto_buy_discovery:
            self.store.mark_auto_buy_discovery_watch(
                candidate.mint,
                status="QUALIFIED",
                reason=(
                    f"{candidate.decision} passed discovery gates; "
                    "awaiting budget and execution gates"
                ),
            )
        seed_raw = round(self.settings.auto_buy_seed_size_usdc * 1_000_000)
        try:
            amount_raw, funding_source = self.store.preview_auto_buy_budget(
                seed_size_usdc_raw=seed_raw,
                max_seed_buys=self.settings.auto_buy_max_seed_buys,
                max_open_positions=(
                    self.settings.auto_buy_max_open_positions
                ),
            )
        except ValueError as exc:
            LOGGER.info("AUTO-BUY WAITING %s (%s)", candidate.symbol, exc)
            return
        event_key = (
            f"solana:{candidate.mint}:auto-buy:{policy['updated_at']}"
        )
        intent = BuyIntent(
            mint=candidate.mint,
            symbol=str(policy["symbol"]),
            event_key=event_key,
            amount_usdc_raw=amount_raw,
            funding_source=funding_source,
        )
        if self.auto_buyer is None:
            if event_key not in self.auto_buy_dry_run_seen:
                LOGGER.warning(
                    "AUTO-BUY READY (DRY RUN) %s decision=%s amount=$%.2f "
                    "funding=%s",
                    intent.symbol,
                    candidate.decision,
                    amount_raw / 1_000_000,
                    funding_source,
                )
                self.auto_buy_dry_run_seen.add(event_key)
            return

        assert self.settings.solana_wallet_address is not None
        rpc = SolanaRpc(self.settings.solana_rpc_http_url)
        try:
            usdc = await rpc.token_balance(
                self.settings.solana_wallet_address, USDC_MINT
            )
            existing = await rpc.token_balance(
                self.settings.solana_wallet_address, candidate.mint
            )
            output_decimals = await rpc.mint_decimals(candidate.mint)
            if usdc.raw_amount < amount_raw:
                raise ValueError("wallet USDC balance is below the buy amount")
            if existing.raw_amount > 0:
                raise ValueError(
                    "wallet already holds this mint; cost-basis mixing blocked"
                )
            simulation = await self.auto_buyer.preflight(intent, rpc)
            prepared = simulation.prepared
        except (ConnectionError, RuntimeError, ValueError) as exc:
            LOGGER.warning("AUTO-BUY NOT SUBMITTED %s (%s)", intent.symbol, exc)
            if self.settings.auto_buy_discovery:
                self.store.mark_auto_buy_discovery_watch(
                    candidate.mint,
                    status="QUALIFIED",
                    reason=f"buy gate blocked submission: {exc}",
                )
            return
        claimed = self.store.begin_auto_buy_execution(
            event_key=event_key,
            token_address=intent.mint,
            symbol=intent.symbol,
            funding_source=funding_source,
            input_usdc_raw=prepared.input_amount_raw,
            expected_output_raw=prepared.expected_output_raw,
        )
        if not claimed:
            return
        try:
            receipt = await self.auto_buyer.execute(prepared)
            if receipt.output_amount_raw <= 0:
                raise RuntimeError("confirmed buy reported no token output")
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
                    token_address=intent.mint,
                    symbol=intent.symbol,
                    quantity=quantity,
                    entry_price=cost_usdc / quantity,
                    price_currency="USD",
                    cost_amount=cost_usdc,
                )
            )
            self.store.arm_auto_sell(intent.mint, reset_stage=True)
            self.store.clear_auto_sell_signal_confirmation(intent.mint)
        except (ConnectionError, RuntimeError, ValueError) as exc:
            self.store.freeze_auto_buy_execution(
                event_key=event_key,
                error=str(exc),
                signature=getattr(exc, "signature", None),
            )
            if self.settings.auto_buy_discovery:
                self.store.mark_auto_buy_discovery_watch(
                    candidate.mint,
                    status="REVIEW",
                    reason=f"execution requires review: {exc}",
                )
            LOGGER.error("AUTO-BUY FROZEN FOR REVIEW %s (%s)", intent.symbol, exc)
            return
        if self.settings.auto_buy_discovery:
            self.store.mark_auto_buy_discovery_watch(
                candidate.mint,
                status="BOUGHT",
                reason=f"confirmed purchase {receipt.signature}",
            )
        LOGGER.warning(
            "AUTO-BUY CONFIRMED %s amount=$%.2f signature=%s",
            intent.symbol,
            receipt.input_amount_raw / 1_000_000,
            receipt.signature,
        )
