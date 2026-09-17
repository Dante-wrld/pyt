from __future__ import annotations

import time
from dataclasses import dataclass

from .core import Position
from .market import MarketQuote


@dataclass(slots=True)
class StrategyState:
    mint: str
    tier: str
    cost_sol: float
    baseline_liquidity_usd: float
    peak_price_sol: float
    last_price_sol: float
    is_open: bool = True
    last_exit_at: float | None = None
    last_exit_price_sol: float | None = None
    reentries: int = 0


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    action: str
    reason: str


class AdaptiveStrategy:
    """Rule-based exit and re-entry confirmation for paper positions."""

    def __init__(
        self,
        *,
        trailing_activation_pct: float = 20,
        trailing_stop_pct: float = 12,
        momentum_exit_pct: float = -8,
        sell_pressure_ratio: float = 1.5,
        liquidity_drop_pct: float = 30,
        reentry_cooldown_seconds: float = 120,
        reentry_momentum_pct: float = 3,
        reentry_buy_sell_ratio: float = 1.4,
        max_reentries: int = 2,
    ) -> None:
        self.trailing_activation_pct = trailing_activation_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.momentum_exit_pct = momentum_exit_pct
        self.sell_pressure_ratio = sell_pressure_ratio
        self.liquidity_drop_pct = liquidity_drop_pct
        self.reentry_cooldown_seconds = reentry_cooldown_seconds
        self.reentry_momentum_pct = reentry_momentum_pct
        self.reentry_buy_sell_ratio = reentry_buy_sell_ratio
        self.max_reentries = max_reentries
        self.states: dict[str, StrategyState] = {}

    def register_open(
        self,
        position: Position,
        quote: MarketQuote,
        tier: str,
        *,
        is_reentry: bool = False,
    ) -> None:
        previous = self.states.get(position.mint)
        reentries = previous.reentries if previous else 0
        if is_reentry:
            reentries += 1
        self.states[position.mint] = StrategyState(
            mint=position.mint,
            tier=tier,
            cost_sol=position.cost_sol,
            baseline_liquidity_usd=quote.liquidity_usd or 0,
            peak_price_sol=quote.price_sol,
            last_price_sol=quote.price_sol,
            is_open=True,
            reentries=reentries,
        )

    def ensure_open(
        self, position: Position, quote: MarketQuote, tier: str = "LEGACY"
    ) -> None:
        if position.mint not in self.states:
            self.register_open(position, quote, tier)

    def evaluate_open(
        self, position: Position, quote: MarketQuote
    ) -> StrategyDecision:
        self.ensure_open(position, quote)
        state = self.states[position.mint]
        state.peak_price_sol = max(state.peak_price_sol, quote.price_sol)
        state.last_price_sol = quote.price_sol

        pnl_pct = (quote.price_sol / position.entry_price_sol - 1) * 100
        peak_gain_pct = (
            state.peak_price_sol / position.entry_price_sol - 1
        ) * 100
        drawdown_from_peak = (
            quote.price_sol / state.peak_price_sol - 1
        ) * 100
        sell_pressure = quote.sells_m5 / max(1, quote.buys_m5)
        liquidity = quote.liquidity_usd or 0
        liquidity_floor = state.baseline_liquidity_usd * (
            1 - self.liquidity_drop_pct / 100
        )

        if (
            peak_gain_pct >= self.trailing_activation_pct
            and drawdown_from_peak <= -self.trailing_stop_pct
        ):
            return StrategyDecision(
                "SELL",
                (
                    f"TRAILING_STOP peak_drawdown={drawdown_from_peak:.2f}% "
                    f"pnl={pnl_pct:.2f}%"
                ),
            )

        momentum_reversal = (
            quote.price_change_m5_pct is not None
            and quote.price_change_m5_pct <= self.momentum_exit_pct
            and sell_pressure >= self.sell_pressure_ratio
            and quote.sells_m5 >= 5
        )
        if momentum_reversal:
            return StrategyDecision(
                "SELL",
                (
                    f"MOMENTUM_REVERSAL change5m={quote.price_change_m5_pct:.2f}% "
                    f"sell_pressure={sell_pressure:.2f}"
                ),
            )

        if (
            state.baseline_liquidity_usd > 0
            and liquidity < liquidity_floor
            and quote.sells_m5 > quote.buys_m5
        ):
            drop = (liquidity / state.baseline_liquidity_usd - 1) * 100
            return StrategyDecision(
                "SELL",
                f"LIQUIDITY_DROP change={drop:.2f}%",
            )

        return StrategyDecision("HOLD", "signals remain inside limits")

    def record_exit(self, mint: str, price_sol: float) -> None:
        state = self.states.get(mint)
        if state is None:
            return
        state.is_open = False
        state.last_exit_at = time.monotonic()
        state.last_exit_price_sol = price_sol
        state.last_price_sol = price_sol

    def evaluate_reentry(self, quote: MarketQuote) -> StrategyDecision:
        state = self.states.get(quote.mint)
        if state is None or state.is_open:
            return StrategyDecision("WAIT", "no closed strategy state")
        if state.reentries >= self.max_reentries:
            return StrategyDecision("WAIT", "maximum reentries reached")
        if state.last_exit_at is None:
            return StrategyDecision("WAIT", "exit timestamp unavailable")

        elapsed = time.monotonic() - state.last_exit_at
        previous_price = state.last_price_sol
        state.last_price_sol = quote.price_sol
        if elapsed < self.reentry_cooldown_seconds:
            return StrategyDecision("WAIT", "reentry cooldown active")

        change = quote.price_change_m5_pct
        buy_sell = quote.buy_sell_ratio
        liquidity = quote.liquidity_usd or 0
        liquidity_ok = liquidity >= state.baseline_liquidity_usd * 0.8
        rising_tick = quote.price_sol > previous_price

        if (
            change is not None
            and change >= self.reentry_momentum_pct
            and buy_sell >= self.reentry_buy_sell_ratio
            and quote.buys_m5 >= 8
            and liquidity_ok
            and rising_tick
        ):
            return StrategyDecision(
                "REENTER",
                (
                    f"RECOVERY change5m={change:.2f}% "
                    f"buy_sell={buy_sell:.2f} liquidity=${liquidity:,.0f}"
                ),
            )
        return StrategyDecision("WAIT", "recovery not confirmed")
