from __future__ import annotations

import logging

from .core import Launch
from .launch_guard_state import LaunchGuardState
from .wallet import WalletTrade

LOGGER = logging.getLogger("solana_launch_guard")


class WalletCopyMixin(LaunchGuardState):
    """Copy-trading a watched wallet's buys."""

    async def handle_wallet_trade(self, trade: WalletTrade) -> None:
        quote = await self.oracle.quote(trade.mint)
        symbol = quote.symbol if quote else trade.mint[:6]
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

        LOGGER.info(
            "TRADER %-4s wallet=%s token=%s mint=%s amount=%.8g signature=%s",
            trade.side,
            trade.wallet,
            symbol,
            trade.mint,
            trade.token_delta,
            trade.signature,
        )

        if trade.side != "BUY":
            return

        rejection: str | None = None
        if self.broker.has_position(trade.mint):
            rejection = "position already exists"
        elif self.broker.open_count >= self.settings.max_open_positions:
            rejection = "maximum open positions reached"
        elif (
            self.broker.exposure_sol + self.settings.trade_size_sol
            > self.settings.max_total_exposure_sol + 1e-12
        ):
            rejection = "maximum total exposure would be exceeded"
        elif quote is None:
            rejection = "no SOL market quote available"
        elif (
            quote.liquidity_usd is None
            or quote.liquidity_usd < self.settings.copy_min_liquidity_usd
        ):
            rejection = (
                f"liquidity below ${self.settings.copy_min_liquidity_usd:,.0f}"
            )

        if rejection:
            LOGGER.info(
                "COPY REJECT token=%s mint=%s reason=%s",
                symbol,
                trade.mint,
                rejection,
            )
            return

        # The `quote is None` branch above already returned, so this is the
        # only remaining path and quote must be set.
        assert quote is not None
        launch = Launch(
            mint=trade.mint,
            name=symbol,
            symbol=symbol,
            creator=None,
            signature=trade.signature,
            virtual_sol=None,
            virtual_tokens=None,
            market_cap_sol=None,
            creator_buy_sol=None,
            price_sol=quote.price_sol,
            received_at="",
            raw={"source": "wallet_copy", "wallet": trade.wallet},
        )
        position = self.broker.open(launch, reason=f"COPY:{trade.wallet}")
        self.strategy.register_open(position, quote, "COPY")
        LOGGER.info(
            "COPY PAPER BUY %-10s mint=%s %.6f SOL at %.12g "
            "SOL/token leader=%s",
            position.symbol,
            position.mint,
            position.cost_sol,
            position.entry_price_sol,
            trade.wallet,
        )
