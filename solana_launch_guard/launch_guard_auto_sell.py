from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import replace

from .execution import PortfolioSignalExitPlanner
from .launch_guard_state import LaunchGuardState
from .portfolio import PortfolioSignal
from .wallet import (
    SolanaRpc,
    SolanaTokenHolding,
)

LOGGER = logging.getLogger("solana_launch_guard")


class AutoSellMixin(LaunchGuardState):
    """Deciding whether and how much of an owned position to sell."""

    async def _maybe_auto_sell(
        self, signal: PortfolioSignal, balance: SolanaTokenHolding
    ) -> None:
        if not self.settings.auto_sell_enabled:
            return
        trial_extra = False
        if os.getenv("AGENT_LIVE_CANARY_ONLY", "false").strip().lower() in {"true", "1", "yes", "on"}:
            from .agent_live_test import canary_exit_allowed
            if not canary_exit_allowed(signal.token_address):
                from .agent_trial_sell import extra_exit_eligible
                trial_extra = extra_exit_eligible(
                    signal, balance,
                    minimum_usd=max(self.settings.auto_sell_min_value_usd,
                                    self.settings.portfolio_min_sell_value_usd),
                )
                if not trial_extra:
                    return
        if signal.token_address in self.settings.auto_sell_excluded_mints:
            self.store.clear_auto_sell_signal_confirmation(
                signal.token_address
            )
            return
        policy = self.store.load_auto_sell_policy(signal.token_address)
        if policy is not None and not bool(policy["armed"]):
            self.store.clear_auto_sell_signal_confirmation(
                signal.token_address
            )
            return
        holding = next(
            (
                item
                for item in self.store.load_owned_holdings("solana")
                if item.token_address == signal.token_address
            ),
            None,
        )
        intent = None
        source = "portfolio signal"
        cycle = self.store.auto_sell_cycle(signal.token_address)
        if (
            not trial_extra
            and
            policy is not None
            and bool(policy["armed"])
            and holding is not None
            and signal.price_currency == "USD"
            and signal.current_price is not None
        ):
            intent = self.profit_ladder.plan(
                mint=signal.token_address,
                symbol=signal.symbol,
                stage=int(policy["stage"]),
                balance_raw=balance.raw_amount,
                decimals=balance.decimals,
                current_price_usd=signal.current_price,
                entry_price_usd=holding.entry_price,
                original_cost_usd=holding.cost_amount,
                cycle=cycle,
            )
            if intent is not None and signal.decision in {
                "TAKE PARTIAL", "PROTECT PROFIT", "EXIT WARNING"
            }:
                source = "profit ladder"
            else:
                intent = None

        portfolio_signal_eligible = (
            intent is None
            and self.settings.auto_sell_portfolio_signals
            and signal.current_value_usd is not None
            and signal.current_value_usd
            >= max(self.settings.auto_sell_min_value_usd, self.settings.portfolio_min_sell_value_usd)
            and signal.decision
            in {"TAKE PARTIAL", "PROTECT PROFIT", "EXIT WARNING"}
        )
        if portfolio_signal_eligible:
            confirmation = self.store.record_auto_sell_signal_confirmation(
                token_address=signal.token_address,
                decision=signal.decision,
                reason=signal.reason,
                observed_at_epoch=time.time(),
                max_gap_seconds=(
                    self.settings.auto_sell_signal_max_gap_seconds
                ),
            )
            polls = int(confirmation["consecutive_polls"])
            required = self.settings.auto_sell_signal_confirmation_polls
            if polls < required:
                LOGGER.warning(
                    "AUTO-SELL CONFIRMING %s decision=%s polls=%d/%d",
                    signal.symbol,
                    signal.decision,
                    polls,
                    required,
                )
                return
            intent = self.portfolio_signal_exit.plan(
                mint=signal.token_address,
                symbol=signal.symbol,
                decision=signal.decision,
                reason=signal.reason,
                balance_raw=balance.raw_amount,
                decimals=balance.decimals,
                cycle=cycle,
            )
        elif intent is None:
            self.store.clear_auto_sell_signal_confirmation(
                signal.token_address
            )
        if intent is None:
            return
        if trial_extra and intent.amount_raw != balance.raw_amount:
            LOGGER.info("EXTRA TRIAL SELL SKIPPED %s: full exit required", intent.symbol)
            return
        minimum_sell_value = max(
            self.settings.auto_sell_min_value_usd,
            self.settings.portfolio_min_sell_value_usd,
        )
        if (signal.current_value_usd is None or balance.raw_amount <= 0
                or signal.current_value_usd * intent.amount_raw / balance.raw_amount
                < minimum_sell_value):
            self.store.clear_auto_sell_signal_confirmation(signal.token_address)
            LOGGER.info("AUTO-SELL SKIPPED %s: estimated sell amount below $%.2f",
                        signal.symbol, minimum_sell_value)
            return
        if self.auto_seller is None:
            if intent.event_key not in self.auto_sell_dry_run_seen:
                LOGGER.warning(
                    "AUTO-SELL READY (DRY RUN) %s source=%s reason=%s",
                    intent.symbol,
                    source,
                    intent.reason,
                )
                self.auto_sell_dry_run_seen.add(intent.event_key)
            return

        batch_key: str | None = None
        batch_full_exit = False
        if (
            source == "portfolio signal"
            and self.settings.auto_sell_adaptive_chunks
            and not trial_extra
        ):
            batch_key = intent.event_key
            batch = self.store.load_or_create_auto_sell_batch(
                batch_key=batch_key,
                chain="solana",
                token_address=intent.mint,
                symbol=intent.symbol,
                stage=intent.stage,
                target_raw=intent.amount_raw,
                full_exit=intent.amount_raw >= intent.balance_raw,
            )
            if batch["status"] != "ACTIVE":
                return
            remaining_raw = int(batch["target_raw"]) - int(batch["sold_raw"])
            requested_raw = min(remaining_raw, balance.raw_amount)
            if requested_raw <= 0:
                return
            chunk_index = int(batch["next_chunk_index"])
            batch_full_exit = bool(batch["full_exit"])
            intent = replace(
                intent,
                event_key=f"{batch_key}:chunk:{chunk_index}",
                amount_raw=requested_raw,
                balance_raw=balance.raw_amount,
                reason=(
                    f"{intent.reason}; adaptive chunk {chunk_index + 1}, "
                    f"remaining target {remaining_raw} raw units"
                ),
            )

        rpc = SolanaRpc(self.settings.solana_rpc_http_url)
        try:
            if batch_key is not None:
                minimum_raw = math.ceil(
                    balance.raw_amount
                    * self.settings.auto_sell_min_chunk_fraction
                )
                simulation = await self.auto_seller.preflight_adaptive(
                    intent,
                    rpc,
                    minimum_amount_raw=minimum_raw,
                    max_attempts=(
                        self.settings.auto_sell_max_chunk_attempts
                    ),
                )
            else:
                simulation = await self.auto_seller.preflight(intent, rpc)
            prepared = simulation.prepared
            if trial_extra and (prepared.expected_output_raw > 5_000_000
                                or prepared.minimum_output_raw < 2_000_000):
                LOGGER.info("EXTRA TRIAL SELL SKIPPED %s: quote outside $2-$5", intent.symbol)
                return
            if prepared.minimum_output_raw < math.ceil(minimum_sell_value * 1_000_000):
                LOGGER.info("AUTO-SELL SKIPPED %s: minimum quoted output below $%.2f",
                            intent.symbol, minimum_sell_value)
                return
            if signal.decision in {"TAKE PARTIAL", "PROTECT PROFIT"}:
                if (holding is None or holding.cost_amount is None or
                    holding.price_currency != "USD" or holding.quantity <= 0 or
                    abs(holding.quantity - balance.amount) / balance.amount >= 0.01):
                    LOGGER.info("AUTO-SELL SKIPPED %s: verified USD cost basis unavailable",
                                intent.symbol)
                    return
                allocated_raw = math.ceil(
                    holding.cost_amount * prepared.input_amount_raw
                    / balance.raw_amount * 1_000_000
                )
                if prepared.minimum_output_raw <= allocated_raw:
                    LOGGER.info("AUTO-SELL SKIPPED %s: minimum quoted proceeds do not cover allocated cost",
                                intent.symbol)
                    return
        except (ConnectionError, RuntimeError, ValueError) as exc:
            LOGGER.warning(
                "AUTO-SELL NOT SUBMITTED %s source=%s (%s)",
                intent.symbol,
                source,
                exc,
            )
            return
        if trial_extra:
            from .agent_live_test import CanaryJournal
            journal = CanaryJournal(os.getenv("AGENT_LIVE_EXTRA_SELL_PATH", "launch_guard_live_extra_sell.json"))
            try:
                journal.claim({"status": "PREPARED", "mint": intent.mint,
                               "at": time.time(), "maximum_proceeds_usd": 5})
            except ValueError:
                return
        claimed = self.store.begin_auto_sell_execution(
            event_key=intent.event_key,
            chain="solana",
            token_address=intent.mint,
            symbol=intent.symbol,
            stage=intent.stage,
            requested_raw=prepared.input_amount_raw,
            expected_output_raw=prepared.expected_output_raw,
            balance_before_raw=intent.balance_raw,
        )
        if not claimed:
            return
        try:
            receipt = await self.auto_seller.execute(prepared)
        except (ConnectionError, RuntimeError, ValueError) as exc:
            failure_signature = getattr(exc, "signature", None)
            if batch_key is not None:
                self.store.freeze_auto_sell_chunk(
                    batch_key=batch_key,
                    event_key=intent.event_key,
                    error=str(exc),
                    signature=failure_signature,
                )
            else:
                self.store.freeze_auto_sell_execution(
                    event_key=intent.event_key,
                    error=str(exc),
                    signature=failure_signature,
                )
            LOGGER.error(
                "AUTO-SELL FROZEN FOR REVIEW %s stage=%d (%s)",
                intent.symbol,
                intent.stage,
                exc,
            )
            if self.push_client is not None:
                try:
                    await self.push_client.send(
                        title="🔴 Launch Guard: SELL NEEDS REVIEW",
                        message=(
                            f"TOKEN: {intent.symbol} • SOLANA\n"
                            f"Stage: {intent.stage + 1}\n"
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
                        "Could not send auto-sell review alert (%s)",
                        notification_exc,
                    )
            return

        batch_complete = False
        if batch_key is not None:
            batch_complete = self.store.complete_auto_sell_chunk(
                batch_key=batch_key,
                event_key=intent.event_key,
                signature=receipt.signature,
                sold_raw=receipt.input_amount_raw,
                output_usdc_raw=receipt.output_amount_raw,
            )
        else:
            self.store.complete_auto_sell_execution(
                event_key=intent.event_key,
                signature=receipt.signature,
                next_stage=intent.stage + 1,
            )
        managed_complete = (
            (source == "profit ladder" and intent.stage + 1 >= 2)
            or (
                batch_key is not None
                and batch_complete
                and batch_full_exit
            )
            or (
                batch_key is None
                and receipt.input_amount_raw >= intent.balance_raw
            )
        )
        reinvestment = self.store.record_auto_buy_sale(
            token_address=intent.mint,
            sold_raw=receipt.input_amount_raw,
            proceeds_usdc_raw=receipt.output_amount_raw,
            reinvest_pct=self.settings.auto_buy_reinvest_profit_pct,
            managed_complete=managed_complete,
        )
        allocated_cost_usd = (
            reinvestment["allocated_cost_usdc_raw"] / 1_000_000
            if reinvestment is not None else None
        )
        if (
            allocated_cost_usd is None and holding is not None
            and holding.price_currency == "USD" and holding.cost_amount is not None
            and holding.quantity > 0 and balance.raw_amount > 0
            and abs(holding.quantity - balance.amount) / balance.amount < 0.01
        ):
            allocated_cost_usd = (
                holding.cost_amount * receipt.input_amount_raw / balance.raw_amount
            )
        sale_proceeds_usd = receipt.output_amount_raw / 1_000_000
        if allocated_cost_usd is not None and allocated_cost_usd > sale_proceeds_usd:
            self.store.record_loss_sale(
                sale_id=receipt.signature, token_address=intent.mint,
                symbol=intent.symbol, cost_usd=allocated_cost_usd,
                proceeds_usd=sale_proceeds_usd,
                quantity=receipt.input_amount_raw / (10 ** intent.decimals),
                sold_at_epoch=time.time(), source="confirmed_auto_sell",
                exit_liquidity_usd=signal.liquidity_usd,
            )
        if (
            self.settings.auto_rebuy_enabled
            and source == "portfolio signal"
            # PROTECT PROFIT and EXIT WARNING are both full, managed exits
            # and equally deserve a rebuy watch. A considerable rise that
            # reverses and gets sold to protect profit is not a mistake to
            # walk away from; it is exactly the case where watching for a
            # lower re-entry matters most. TAKE PARTIAL is excluded: it is a
            # partial sell, not a closed position. Read from _STAGES rather
            # than hardcoded numbers so a reordering there can't silently
            # desync this check.
            and intent.stage in (
                PortfolioSignalExitPlanner._STAGES["PROTECT PROFIT"],
                PortfolioSignalExitPlanner._STAGES["EXIT WARNING"],
            )
            and managed_complete
            and intent.mint not in self.settings.auto_buy_excluded_mints
        ):
            sold_raw = receipt.input_amount_raw
            proceeds_raw = receipt.output_amount_raw
            sell_signature = receipt.signature
            if batch_key is not None and batch_complete:
                completed_batch = self.store.load_auto_sell_batch(batch_key)
                if completed_batch is not None:
                    sold_raw = int(completed_batch["sold_raw"])
                    proceeds_raw = int(
                        completed_batch["proceeds_usdc_raw"]
                    )
                    sell_signature = str(
                        completed_batch["last_signature"]
                        or receipt.signature
                    )
            sold_tokens = sold_raw / (10**intent.decimals)
            if sold_tokens > 0 and proceeds_raw > 0:
                watch = self.store.start_auto_rebuy_watch(
                    token_address=intent.mint,
                    symbol=intent.symbol,
                    sell_signature=sell_signature,
                    exit_price_usd=(proceeds_raw / 1_000_000) / sold_tokens,
                    exit_liquidity_usd=signal.liquidity_usd,
                    sale_proceeds_usdc_raw=proceeds_raw,
                    sold_at_epoch=time.time(),
                    max_rebuys=self.settings.auto_rebuy_max_per_token,
                )
                if watch is not None:
                    self.store.clear_auto_sell_signal_confirmation(intent.mint)
                    LOGGER.warning(
                        "AUTO-REBUY WATCHING %s cycle=%d exit=$%.12g",
                        intent.symbol,
                        int(watch["cycle"]),
                        float(watch["exit_price_usd"]),
                    )
        LOGGER.warning(
            "AUTO-SELL CONFIRMED %s source=%s input_raw=%d "
            "adaptive_attempts=%d batch_complete=%s signature=%s",
            intent.symbol,
            source,
            receipt.input_amount_raw,
            simulation.adaptive_attempts,
            batch_complete,
            receipt.signature,
        )
        if self.push_client is not None:
            try:
                await self.push_client.send(
                    title="🟢 Launch Guard: SELL CONFIRMED",
                    message=(
                        f"TOKEN: {intent.symbol} • SOLANA\n"
                        f"Source: {source}\n"
                        f"Reason: {intent.reason}\n"
                        f"Signature: {receipt.signature}"
                    ),
                    url=f"https://solscan.io/tx/{receipt.signature}",
                    url_title="Open Solscan",
                    sound="cashregister",
                    priority=1,
                )
            except ConnectionError as notification_exc:
                LOGGER.warning(
                    "Sell confirmed, but the phone alert failed (%s)",
                    notification_exc,
                )

        if reinvestment is not None:
            LOGGER.warning(
                "AUTO-BUY PROFIT LEDGER %s realized=$%.6f reinvested=$%.6f",
                intent.symbol,
                reinvestment["profit_usdc_raw"] / 1_000_000,
                reinvestment["reinvest_credit_usdc_raw"] / 1_000_000,
            )
