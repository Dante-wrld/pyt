from __future__ import annotations

from dataclasses import dataclass

from .market import MarketQuote


@dataclass(frozen=True, slots=True)
class IntelligenceResult:
    tier: str
    total_score: int
    safety_score: int
    momentum_score: int
    reasons: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return self.tier in {"CORE", "MOONSHOT"}


class CoinIntelligence:
    """Explainable heuristic ranking; it is not a profit prediction model."""

    def __init__(
        self,
        *,
        core_score: int = 75,
        moonshot_score: int = 60,
        hard_min_liquidity_usd: float = 5_000,
        core_min_liquidity_usd: float = 20_000,
        max_market_cap_liquidity_ratio: float = 30,
        moonshot_max_market_cap_usd: float = 500_000,
    ) -> None:
        self.core_score = core_score
        self.moonshot_score = moonshot_score
        self.hard_min_liquidity_usd = hard_min_liquidity_usd
        self.core_min_liquidity_usd = core_min_liquidity_usd
        self.max_market_cap_liquidity_ratio = max_market_cap_liquidity_ratio
        self.moonshot_max_market_cap_usd = moonshot_max_market_cap_usd

    def score(self, quote: MarketQuote | None) -> IntelligenceResult:
        if quote is None:
            return IntelligenceResult(
                "REJECT", 0, 0, 0, ("market data unavailable",)
            )

        reasons: list[str] = []
        liquidity = quote.liquidity_usd or 0
        ratio = quote.market_cap_to_liquidity

        if liquidity < self.hard_min_liquidity_usd:
            return IntelligenceResult(
                "REJECT",
                0,
                0,
                0,
                (
                    f"liquidity ${liquidity:,.0f} below hard minimum "
                    f"${self.hard_min_liquidity_usd:,.0f}",
                ),
            )
        if ratio is None:
            return IntelligenceResult(
                "REJECT", 0, 0, 0, ("market-cap/liquidity ratio unavailable",)
            )
        if ratio > self.max_market_cap_liquidity_ratio:
            return IntelligenceResult(
                "REJECT",
                0,
                0,
                0,
                (
                    f"market-cap/liquidity ratio {ratio:.1f} above "
                    f"{self.max_market_cap_liquidity_ratio:.1f}",
                ),
            )

        safety = 0
        if liquidity >= 100_000:
            safety += 18
        elif liquidity >= 50_000:
            safety += 15
        elif liquidity >= 20_000:
            safety += 12
        elif liquidity >= 10_000:
            safety += 8
        else:
            safety += 5

        if ratio <= 3:
            safety += 16
        elif ratio <= 6:
            safety += 13
        elif ratio <= 10:
            safety += 10
        elif ratio <= 20:
            safety += 5

        if quote.sells_m5 >= 10:
            safety += 8
        elif quote.sells_m5 >= 3:
            safety += 5
        else:
            reasons.append("very few observed sellers")

        if quote.market_cap_usd is not None:
            safety += 4
        if quote.price_change_m5_pct is not None:
            safety += 4

        buy_sell = quote.buy_sell_ratio
        if 0.5 <= buy_sell <= 5:
            safety += 8
        elif buy_sell <= 10:
            safety += 3
            reasons.append("one-sided buying activity")
        else:
            reasons.append("extremely one-sided buying activity")
        safety = min(50, safety)

        momentum = 0
        if quote.buys_m5 >= 100:
            momentum += 18
        elif quote.buys_m5 >= 50:
            momentum += 15
        elif quote.buys_m5 >= 20:
            momentum += 10
        elif quote.buys_m5 >= 8:
            momentum += 5
        else:
            reasons.append("low five-minute buyer count")

        if 1.2 <= buy_sell <= 3.5:
            momentum += 12
        elif 0.8 <= buy_sell <= 5:
            momentum += 8
        elif buy_sell <= 10:
            momentum += 4

        volume_ratio = quote.volume_liquidity_ratio
        if volume_ratio is None:
            reasons.append("volume/liquidity ratio unavailable")
        elif 0.1 <= volume_ratio <= 1.5:
            momentum += 12
        elif 0.03 <= volume_ratio < 0.1:
            momentum += 7
        elif 1.5 < volume_ratio <= 3:
            momentum += 6
            reasons.append("very high turnover")
        elif volume_ratio > 3:
            momentum += 2
            reasons.append("potentially overheated turnover")

        change = quote.price_change_m5_pct
        if change is None:
            reasons.append("five-minute price change unavailable")
        elif 5 <= change <= 50:
            momentum += 8
        elif 0 <= change < 5:
            momentum += 5
        elif 50 < change <= 120:
            momentum += 4
            reasons.append("rapid recent price increase")
        elif change > 120:
            reasons.append("extreme recent price increase")
        else:
            reasons.append("negative five-minute momentum")

        if quote.pair_created_at_ms is not None:
            momentum += 5
        momentum = min(50, momentum)
        total = safety + momentum

        tier = "REJECT"
        if (
            total >= self.core_score
            and safety >= 35
            and liquidity >= self.core_min_liquidity_usd
        ):
            tier = "CORE"
        elif (
            total >= self.moonshot_score
            and safety >= 25
            and quote.market_cap_usd is not None
            and quote.market_cap_usd <= self.moonshot_max_market_cap_usd
        ):
            tier = "MOONSHOT"

        reasons.insert(
            0,
            (
                f"liquidity=${liquidity:,.0f}, "
                f"market_cap=${(quote.market_cap_usd or 0):,.0f}, "
                f"buys/sells={quote.buys_m5}/{quote.sells_m5}, "
                f"volume5m=${quote.volume_m5_usd:,.0f}"
            ),
        )
        return IntelligenceResult(
            tier=tier,
            total_score=total,
            safety_score=safety,
            momentum_score=momentum,
            reasons=tuple(reasons),
        )
