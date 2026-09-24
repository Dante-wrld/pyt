from __future__ import annotations

import logging
import asyncio
import time
from collections.abc import Mapping
from dataclasses import replace

from .core import Launch
from .launch_guard_state import LaunchGuardState
from .market import MarketQuote
from .portfolio import (
    OwnedHolding,
    build_portfolio_snapshot,
    write_portfolio_snapshot,
)
from .rebuy_assessment import auto_rebuy_recovery_assessment
from .wallet import (
    SolanaRpc,
    SolanaTokenHolding,
)

LOGGER = logging.getLogger("solana_launch_guard")


class PortfolioMonitorMixin(LaunchGuardState):
    """Polling owned positions, pricing them, and routing them to the sell/rebuy checks."""

    async def run_price_monitor(self) -> None:
        LOGGER.info(
            "Adaptive price monitor active for %d restored/open positions",
            self.broker.open_count,
        )
        while True:
            open_positions = [
                position
                for position in self.broker.positions.values()
                if position.status == "OPEN"
            ]
            for position in open_positions:
                quote = await self.oracle.quote(position.mint)
                if quote is None:
                    continue

                self.strategy.ensure_open(position, quote)
                updated = self.broker.mark(position.mint, quote.price_sol)
                if updated is None:
                    continue

                pnl_pct = (
                    quote.price_sol / updated.entry_price_sol - 1
                ) * 100

                if updated.status == "OPEN":
                    decision = self.strategy.evaluate_open(updated, quote)
                    if decision.action == "SELL":
                        updated = self.broker.close(
                            updated.mint,
                            quote.price_sol,
                            decision.reason,
                        )
                        if updated is not None:
                            self.strategy.record_exit(
                                updated.mint, quote.price_sol
                            )
                else:
                    self.strategy.record_exit(updated.mint, quote.price_sol)

                if updated is None:
                    continue
                LOGGER.info(
                    "MARK %-10s mint=%s price=%.12g pnl=%+.2f%% status=%s",
                    updated.symbol,
                    updated.mint,
                    quote.price_sol,
                    pnl_pct,
                    updated.status,
                )
                if updated.status == "CLOSED":
                    LOGGER.info(
                        "ADAPTIVE PAPER SELL %-10s mint=%s reason=%s "
                        "pnl=%+.6f SOL (%+.2f%%)",
                        updated.symbol,
                        updated.mint,
                        updated.exit_reason,
                        updated.pnl_sol or 0,
                        updated.pnl_pct or 0,
                    )

            await self.evaluate_reentries()
            await asyncio.sleep(self.settings.price_poll_seconds)

    async def evaluate_reentries(self) -> None:
        closed_states = [
            state for state in self.strategy.states.values() if not state.is_open
        ]
        if not closed_states:
            return

        sol_usd = await self.oracle.sol_usd_price()
        for state in closed_states:
            quote = await self.oracle.quote(state.mint)
            if quote is None:
                continue
            decision = self.strategy.evaluate_reentry(quote)
            if decision.action != "REENTER":
                continue

            if self.broker.open_count >= self.settings.max_open_positions:
                continue
            if sol_usd is not None:
                exposure_usd = self.broker.exposure_sol * sol_usd
                reentry_usd = state.cost_sol * sol_usd
                if (
                    exposure_usd + reentry_usd
                    > self.settings.max_total_exposure_usd
                ):
                    continue

            launch = Launch(
                mint=state.mint,
                name=quote.symbol,
                symbol=quote.symbol,
                creator=None,
                signature=None,
                virtual_sol=None,
                virtual_tokens=None,
                market_cap_sol=None,
                creator_buy_sol=None,
                price_sol=quote.price_sol,
                received_at="",
                raw={"source": "adaptive_reentry"},
            )
            if state.tier == "MOONSHOT":
                take_profit = self.settings.moonshot_take_profit_pct
                stop_loss = self.settings.moonshot_stop_loss_pct
            else:
                take_profit = self.settings.take_profit_pct
                stop_loss = self.settings.stop_loss_pct

            position = self.broker.open(
                launch,
                reason=f"REENTRY:{state.tier}:{decision.reason}",
                cost_sol=state.cost_sol,
                take_profit_pct=take_profit,
                stop_loss_pct=stop_loss,
            )
            self.strategy.register_open(
                position, quote, state.tier, is_reentry=True
            )
            LOGGER.info(
                "ADAPTIVE REENTRY %-10s mint=%s cost=%.6f SOL reason=%s",
                position.symbol,
                position.mint,
                position.cost_sol,
                decision.reason,
            )

    async def import_fomo_position(
        self,
        *,
        mint: str,
        symbol: str,
        token_amount: float,
        cost_usd: float,
    ) -> None:
        if token_amount <= 0 or cost_usd <= 0:
            raise ValueError("token amount and cost USD must be positive")

        quote, sol_usd = await asyncio.gather(
            self.oracle.quote(mint),
            self.oracle.sol_usd_price(),
        )
        if quote is None:
            raise ValueError("no SOL market quote found for this mint")
        if sol_usd is None:
            raise ValueError("SOL/USD price is unavailable")

        entry_price_usd = cost_usd / token_amount
        entry_price_sol = entry_price_usd / sol_usd
        launch = Launch(
            mint=mint,
            name=symbol,
            symbol=symbol,
            creator=None,
            signature=None,
            virtual_sol=None,
            virtual_tokens=None,
            market_cap_sol=None,
            creator_buy_sol=None,
            price_sol=entry_price_sol,
            received_at="",
            raw={"source": "fomo_manual_import"},
        )
        position = self.broker.positions.get(mint)
        if position is None or position.status != "OPEN":
            position = self.broker.open(
                launch,
                reason="FOMO_MANUAL_IMPORT",
                cost_sol=cost_usd / sol_usd,
            )
            self.strategy.register_open(position, quote, "IMPORTED")
        self.store.save_owned_holding(
            OwnedHolding(
                chain="solana",
                token_address=mint,
                symbol=symbol,
                quantity=token_amount,
                entry_price=entry_price_usd,
                price_currency="USD",
                cost_amount=cost_usd,
            )
        )
        LOGGER.info(
            "SAVED READ-ONLY HOLDING %-10s mint=%s tokens=%.8g "
            "cost=$%.2f entry=$%.12g current=$%.12g",
            symbol,
            mint,
            token_amount,
            cost_usd,
            entry_price_usd,
            quote.price_sol * sol_usd,
        )

    async def run_portfolio_monitor(self) -> None:
        wallet = self.settings.solana_wallet_address
        rpc = SolanaRpc(self.settings.solana_rpc_http_url)
        LOGGER.info(
            "Read-only holdings monitor active%s (refresh %.0fs)",
            f" for {wallet}" if wallet else " for imported holdings",
            self.settings.portfolio_poll_seconds,
        )
        backoff = 1.0
        while True:
            try:
                balances_by_mint: dict[str, SolanaTokenHolding] = {}
                saved = {
                    item.token_address: item
                    for item in self.store.load_owned_holdings("solana")
                }
                if wallet:
                    balances = await rpc.token_holdings(wallet)
                    balances_by_mint = {
                        balance.mint: balance for balance in balances
                    }
                    holdings = []
                    for balance in balances:
                        basis = saved.get(balance.mint)
                        holdings.append(
                            OwnedHolding(
                                chain="solana",
                                token_address=balance.mint,
                                symbol=(
                                    basis.symbol
                                    if basis is not None
                                    else balance.mint[:8]
                                ),
                                quantity=balance.amount,
                                entry_price=(
                                    basis.entry_price if basis is not None else None
                                ),
                                price_currency=(
                                    basis.price_currency if basis is not None else None
                                ),
                                cost_amount=(
                                    basis.cost_amount if basis is not None else None
                                ),
                            )
                        )
                else:
                    holdings = list(saved.values())

                if wallet:
                    reconciled = self.store.reconcile_stale_auto_buy_positions(
                        chain="solana", held_mints=frozenset(balances_by_mint)
                    )
                    for row in reconciled:
                        LOGGER.info(
                            "AUTO-BUY RECONCILED %s mint=%s (wallet no longer holds "
                            "this token; sold outside auto-buy's own tracked paths)",
                            row["symbol"], row["token_address"],
                        )

                if self.settings.auto_rebuy_enabled:
                    await self._monitor_auto_rebuys(balances_by_mint, rpc)
                await self._monitor_loss_sales(balances_by_mint)

                semaphore = asyncio.Semaphore(5)
                # Solana holdings (the overwhelming majority - currently
                # ~75) go through one batched tokens/v1 call instead of one
                # /latest/dex/tokens/{mint} request each: that single-token
                # endpoint sits on a much tighter DexScreener rate-limit
                # bucket than the batch one, and this scan alone was
                # generating most of this process's request volume against
                # it (confirmed live 2026-09-24: consistent 429s from this
                # machine's combined polling). Non-solana holdings are rare
                # enough to keep on the old per-mint path.
                solana_mints = [
                    holding.token_address for holding in holdings
                    if holding.chain == "solana"
                ]
                solana_quotes = (
                    await self.oracle.quote_many(solana_mints, chain="solana")
                    if solana_mints else {}
                )

                async def evaluate(holding: OwnedHolding):
                    if holding.chain == "solana":
                        return holding, solana_quotes.get(holding.token_address)
                    async with semaphore:
                        quote = await self.oracle.quote(
                            holding.token_address, chain=holding.chain
                        )
                    return holding, quote

                results = await asyncio.gather(
                    *(evaluate(holding) for holding in holdings)
                )
                sol_usd = await self.oracle.sol_usd_price() if results else None
                signals = [
                    self.portfolio_advisor.evaluate(
                        holding, quote, sol_usd=sol_usd
                    )
                    for holding, quote in results
                ]
                # Add candle evidence only to an existing advisory rebound watch.
                # Exit research is visible to agents but does not replace executable guidance.
                for index, (holding, quote) in enumerate(results):
                    if holding.chain == "solana" and quote is not None:
                        signals[index].exit_research = self.structure_scanner.exit_research(
                            pool=quote.pair_address, mint=holding.token_address,
                        )
                # Sell and profit-protection decisions retain their precedence.
                for index, (holding, quote) in enumerate(results):
                    if (
                        signals[index].decision != "REBOUND WATCH"
                        or holding.chain != "solana"
                        or quote is None
                        or quote.pair_created_at_ms is None
                    ):
                        continue
                    evidence = await self.structure_scanner.scan(
                        pool=quote.pair_address,
                        mint=holding.token_address,
                        age_seconds=max(
                            0, (time.time() * 1000 - quote.pair_created_at_ms) / 1000
                        ),
                    )
                    if (
                        evidence is not None
                        and quote.price_usd is not None
                        and quote.price_usd > 0
                        and abs(evidence.entry / quote.price_usd - 1) <= 0.15
                    ):
                        signals[index] = replace(
                            signals[index],
                            decision="STRUCTURE WATCH",
                            reason=evidence.description(),
                        )
                for signal in signals:
                    state = self.portfolio_advisor.state_for(
                        signal.chain, signal.token_address
                    )
                    if state is not None:
                        peak_price, baseline_liquidity = state
                        self.store.save_portfolio_state(
                            chain=signal.chain,
                            token_address=signal.token_address,
                            peak_price=peak_price,
                            baseline_liquidity_usd=baseline_liquidity,
                            below_sell_minimum=self.portfolio_advisor.below_sell_minimum_for(
                                signal.chain, signal.token_address
                            ),
                        )
                signals = [
                    signal
                    for signal in signals
                    if signal.current_value_usd is None
                    or signal.current_value_usd
                    >= self.settings.portfolio_min_value_usd
                ]
                for signal in signals:
                    key = f"{signal.chain}:{signal.token_address.casefold()}"
                    previous = self.portfolio_last_decisions.get(key)
                    if previous != signal.decision:
                        LOGGER.info(
                            "PORTFOLIO %-14s %-10s mint=%s reason=%s",
                            signal.decision,
                            signal.symbol,
                            signal.token_address,
                            signal.reason,
                        )
                        self.portfolio_last_decisions[key] = signal.decision

                    if self.portfolio_notifier is not None:
                        try:
                            sent = await self.portfolio_notifier.maybe_send(
                                signal
                            )
                        except ConnectionError as exc:
                            LOGGER.warning(
                                "Portfolio phone alert unavailable for %s (%s)",
                                signal.symbol,
                                exc,
                            )
                        else:
                            if sent:
                                LOGGER.info(
                                    "HIGH PRIORITY PHONE ALERT %s "
                                    "decision=%s",
                                    signal.symbol,
                                    signal.decision,
                                )

                    owned_balance = balances_by_mint.get(signal.token_address)
                    if owned_balance is not None:
                        await self._maybe_auto_sell(signal, owned_balance)

                write_portfolio_snapshot(
                    self.settings.portfolio_snapshot_path,
                    build_portfolio_snapshot(
                        signals,
                        wallet=wallet,
                        poll_seconds=self.settings.portfolio_poll_seconds,
                        loss_sale_reviews=self.store.loss_sale_reviews(),
                        execution_mode=(
                            "live"
                            if self.auto_seller is not None
                            else (
                                "dry-run"
                                if self.settings.auto_sell_enabled
                                else "read-only"
                            )
                        ),
                    ),
                )
                backoff = 1.0
                await asyncio.sleep(self.settings.portfolio_poll_seconds)
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError, ValueError) as exc:
                LOGGER.warning(
                    "Portfolio monitor unavailable (%s); retrying in %.0fs",
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _monitor_loss_sales(
        self, balances_by_mint: Mapping[str, SolanaTokenHolding]
    ) -> None:
        reviews = self.store.loss_sale_reviews()
        if not reviews:
            return
        semaphore = asyncio.Semaphore(5)

        async def observe(mint: str) -> tuple[str, MarketQuote | None]:
            async with semaphore:
                return mint, await self.oracle.quote(mint, chain="solana")

        quotes = dict(await asyncio.gather(*(
            observe(mint) for mint in {row["token_address"] for row in reviews}
        )))
        now = time.time()
        for review in reviews:
            mint = str(review["token_address"])
            if mint in balances_by_mint and balances_by_mint[mint].raw_amount > 0:
                self.store.reset_loss_sale_review(
                    str(review["sale_id"]), "wallet already holds this token"
                )
                continue
            quote = quotes[mint]
            if quote is None or quote.price_usd is None or quote.price_usd <= 0:
                self.store.reset_loss_sale_review(
                    str(review["sale_id"]), "fresh USD market quote unavailable"
                )
                continue
            watch = {**review,
                     "exit_price_usd": float(review["proceeds_usd"]) / float(review["quantity"])}
            qualifying, reason, _ = auto_rebuy_recovery_assessment(
                watch, quote, self.settings, now=now, advisory=True
            )
            self.store.update_loss_sale_review(
                str(review["sale_id"]), price_usd=quote.price_usd,
                qualified=qualifying, reason=reason,
                confirmation_required=self.settings.auto_rebuy_confirmation_polls,
            )
