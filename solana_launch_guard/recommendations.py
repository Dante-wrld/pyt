from __future__ import annotations

import time
from dataclasses import dataclass

from .intelligence import IntelligenceResult
from .market import MarketQuote


@dataclass(slots=True)
class RecommendationCandidate:
    mint: str
    symbol: str
    tier: str
    intelligence_score: int
    initial_price_sol: float
    current_price_sol: float
    liquidity_usd: float | None
    price_change_m5_pct: float | None
    buy_sell_ratio: float
    observed_at: float
    updated_at: float

    @property
    def rise_pct(self) -> float:
        return (self.current_price_sol / self.initial_price_sol - 1.0) * 100.0

    @property
    def signal_score(self) -> int:
        """Blend initial quality with bounded live momentum.

        The intelligence score remains the majority of the signal. Price changes
        are deliberately capped so a brief pump cannot dominate the ranking.
        """
        rise_component = max(-20.0, min(25.0, self.rise_pct)) * 0.40
        m5_change = self.price_change_m5_pct or 0.0
        momentum_component = max(-20.0, min(25.0, m5_change)) * 0.20
        flow_component = 3.0 if 1.2 <= self.buy_sell_ratio <= 3.5 else 0.0
        score = self.intelligence_score + rise_component + momentum_component
        score += flow_component
        return max(0, min(100, round(score)))


class RecommendationBook:
    """In-memory shortlist of intelligence-qualified paper candidates."""

    def __init__(self, *, pool_size: int = 30, ttl_seconds: float = 1800) -> None:
        self.pool_size = pool_size
        self.ttl_seconds = ttl_seconds
        self.candidates: dict[str, RecommendationCandidate] = {}

    def add(
        self,
        quote: MarketQuote,
        result: IntelligenceResult,
        *,
        now: float | None = None,
    ) -> RecommendationCandidate | None:
        if not result.accepted or quote.price_sol <= 0:
            return None
        timestamp = time.monotonic() if now is None else now
        existing = self.candidates.get(quote.mint)
        if existing is not None:
            self.update(quote, now=timestamp)
            return existing

        candidate = RecommendationCandidate(
            mint=quote.mint,
            symbol=quote.symbol,
            tier=result.tier,
            intelligence_score=result.total_score,
            initial_price_sol=quote.price_sol,
            current_price_sol=quote.price_sol,
            liquidity_usd=quote.liquidity_usd,
            price_change_m5_pct=quote.price_change_m5_pct,
            buy_sell_ratio=quote.buy_sell_ratio,
            observed_at=timestamp,
            updated_at=timestamp,
        )
        self.candidates[quote.mint] = candidate
        self._trim()
        return candidate

    def update(
        self, quote: MarketQuote, *, now: float | None = None
    ) -> RecommendationCandidate | None:
        candidate = self.candidates.get(quote.mint)
        if candidate is None or quote.price_sol <= 0:
            return None
        candidate.symbol = quote.symbol
        candidate.current_price_sol = quote.price_sol
        candidate.liquidity_usd = quote.liquidity_usd
        candidate.price_change_m5_pct = quote.price_change_m5_pct
        candidate.buy_sell_ratio = quote.buy_sell_ratio
        candidate.updated_at = time.monotonic() if now is None else now
        return candidate

    def expire(self, *, now: float | None = None) -> None:
        timestamp = time.monotonic() if now is None else now
        expired = [
            mint
            for mint, candidate in self.candidates.items()
            if timestamp - candidate.observed_at > self.ttl_seconds
        ]
        for mint in expired:
            del self.candidates[mint]

    def ranked(self, limit: int = 10) -> list[RecommendationCandidate]:
        return sorted(
            self.candidates.values(),
            key=lambda item: (
                item.signal_score,
                item.rise_pct,
                item.intelligence_score,
            ),
            reverse=True,
        )[:limit]

    def _trim(self) -> None:
        if len(self.candidates) <= self.pool_size:
            return
        lowest = min(
            self.candidates.values(),
            key=lambda item: (item.signal_score, item.observed_at),
        )
        del self.candidates[lowest.mint]


def format_recommendations(
    candidates: list[RecommendationCandidate], *, color: bool = True
) -> str:
    gold = "\033[38;5;220m" if color else ""
    reset = "\033[0m" if color else ""
    lines = [
        (
            f"{gold}PAPER RECOMMENDED BUYS 1-{len(candidates)} "
            "(ranked model signals; no profit guarantee)"
        )
    ]
    for rank, item in enumerate(candidates, start=1):
        liquidity = (
            f"${item.liquidity_usd:,.0f}"
            if item.liquidity_usd is not None
            else "unknown"
        )
        m5_change = item.price_change_m5_pct or 0.0
        lines.append(
            f"#{rank:02d} {item.symbol:<10} tier={item.tier:<8} "
            f"signal={item.signal_score:3d} rise={item.rise_pct:+7.2f}% "
            f"m5={m5_change:+7.2f}% liquidity={liquidity} "
            f"price={item.current_price_sol:.12g} mint={item.mint}"
        )
    lines.append(reset)
    return "\n".join(lines)
