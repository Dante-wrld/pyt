from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .intelligence import IntelligenceResult
from .market import MarketQuote

CHAIN_LABELS = {
    "solana": "SOL",
    "ethereum": "ETH",
    "base": "BASE",
    "bob": "BOB",
    "monad": "MON",
    "robinhood": "RH",
    "hyperevm": "HEVM",
}


@dataclass(slots=True)
class RecommendationCandidate:
    mint: str
    symbol: str
    chain: str
    tier: str
    intelligence_score: int
    initial_price: float
    current_price: float
    price_currency: str
    liquidity_usd: float | None
    price_change_m5_pct: float | None
    buy_sell_ratio: float
    observed_at: float
    updated_at: float

    @property
    def rise_pct(self) -> float:
        return (self.current_price / self.initial_price - 1.0) * 100.0

    @property
    def key(self) -> str:
        address = self.mint if self.chain == "solana" else self.mint.lower()
        return f"{self.chain}:{address}"

    @property
    def fomo_url(self) -> str | None:
        if self.chain != "robinhood":
            return None
        return f"https://fomo.family/tokens/robinhood/{self.mint}"

    @property
    def market_url(self) -> str:
        return f"https://dexscreener.com/{self.chain}/{self.mint}"

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
        price = quote.recommendation_price
        if not result.accepted or price <= 0:
            return None
        timestamp = time.monotonic() if now is None else now
        existing = self.candidates.get(quote.recommendation_key)
        if existing is not None:
            self.update(quote, now=timestamp)
            return existing

        candidate = RecommendationCandidate(
            mint=quote.mint,
            symbol=quote.symbol,
            chain=quote.chain,
            tier=result.tier,
            intelligence_score=result.total_score,
            initial_price=price,
            current_price=price,
            price_currency=quote.recommendation_currency,
            liquidity_usd=quote.liquidity_usd,
            price_change_m5_pct=quote.price_change_m5_pct,
            buy_sell_ratio=quote.buy_sell_ratio,
            observed_at=timestamp,
            updated_at=timestamp,
        )
        self.candidates[quote.recommendation_key] = candidate
        self._trim()
        return candidate

    def update(
        self, quote: MarketQuote, *, now: float | None = None
    ) -> RecommendationCandidate | None:
        candidate = self.candidates.get(quote.recommendation_key)
        price = quote.recommendation_price
        if candidate is None or price <= 0:
            return None
        candidate.symbol = quote.symbol
        candidate.current_price = price
        candidate.liquidity_usd = quote.liquidity_usd
        candidate.price_change_m5_pct = quote.price_change_m5_pct
        candidate.buy_sell_ratio = quote.buy_sell_ratio
        candidate.updated_at = time.monotonic() if now is None else now
        return candidate

    def expire(self, *, now: float | None = None) -> None:
        timestamp = time.monotonic() if now is None else now
        expired = [
            key
            for key, candidate in self.candidates.items()
            if timestamp - candidate.observed_at > self.ttl_seconds
        ]
        for key in expired:
            del self.candidates[key]

    def ranked(self, limit: int = 10) -> list[RecommendationCandidate]:
        ordered = sorted(
            self.candidates.values(),
            key=lambda item: (
                item.signal_score,
                item.rise_pct,
                item.intelligence_score,
            ),
            reverse=True,
        )
        unique: list[RecommendationCandidate] = []
        seen_mints: set[str] = set()
        seen_symbols: set[str] = set()
        for candidate in ordered:
            symbol_key = candidate.symbol.strip().casefold()
            mint_key = (
                candidate.mint
                if candidate.chain == "solana"
                else candidate.mint.casefold()
            )
            if mint_key in seen_mints or symbol_key in seen_symbols:
                continue
            unique.append(candidate)
            seen_mints.add(mint_key)
            seen_symbols.add(symbol_key)
            if len(unique) >= limit:
                break
        return unique

    def _trim(self) -> None:
        if len(self.candidates) <= self.pool_size:
            return
        lowest = min(
            self.candidates.values(),
            key=lambda item: (item.signal_score, item.observed_at),
        )
        del self.candidates[lowest.key]


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
        chain_label = CHAIN_LABELS.get(item.chain, item.chain.upper()[:5])
        price_prefix = "$" if item.price_currency == "USD" else ""
        address_label = "mint" if item.chain == "solana" else "contract"
        lines.append(
            f"#{rank:02d} {item.symbol:<10} tier={item.tier:<8} "
            f"chain={chain_label:<3} "
            f"signal={item.signal_score:3d} rise={item.rise_pct:+7.2f}% "
            f"m5={m5_change:+7.2f}% liquidity={liquidity} "
            f"price={price_prefix}{item.current_price:.12g} "
            f"{address_label}={item.mint}"
        )
        if item.fomo_url:
            lines.append(f"    fomo={item.fomo_url}")
        lines.append(f"    market={item.market_url}")
    lines.append(reset)
    return "\n".join(lines)


def build_snapshot(
    candidates: list[RecommendationCandidate],
    *,
    pending_count: int,
    poll_seconds: float,
) -> dict[str, Any]:
    return {
        "generated_at": time.time(),
        "pending_count": pending_count,
        "poll_seconds": poll_seconds,
        "candidates": [
            {
                "rank": rank,
                "mint": candidate.mint,
                "symbol": candidate.symbol,
                "chain": candidate.chain,
                "tier": candidate.tier,
                "signal_score": candidate.signal_score,
                "rise_pct": candidate.rise_pct,
                "price_change_m5_pct": candidate.price_change_m5_pct or 0.0,
                "liquidity_usd": candidate.liquidity_usd,
                "price": candidate.current_price,
                "price_currency": candidate.price_currency,
                "fomo_url": candidate.fomo_url,
                "market_url": candidate.market_url,
            }
            for rank, candidate in enumerate(candidates, start=1)
        ],
    }


def write_snapshot(path: str | Path, snapshot: dict[str, Any]) -> None:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(snapshot, separators=(",", ":")), encoding="utf-8"
    )
    temporary.replace(target)


def read_snapshot(path: str | Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def format_dashboard(snapshot: dict[str, Any], *, color: bool = True) -> str:
    palette = (45, 220, 82, 213, 208, 117, 226, 141, 51, 203)
    reset = "\033[0m" if color else ""
    bold = "\033[1m" if color else ""
    dim = "\033[2m" if color else ""
    generated_at = float(snapshot.get("generated_at") or 0.0)
    poll_seconds = float(snapshot.get("poll_seconds") or 15.0)
    age = max(0.0, time.time() - generated_at)
    stale = age > poll_seconds * 2 + 5
    updated = (
        datetime.fromtimestamp(generated_at, tz=timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S")
        if generated_at
        else "waiting"
    )
    pending = int(snapshot.get("pending_count") or 0)
    raw_candidates = snapshot.get("candidates")
    candidates = raw_candidates if isinstance(raw_candidates, list) else []

    status = "STALE — scanner is not updating" if stale else "LIVE"
    lines = [
        f"{bold}LAUNCH GUARD — PAPER BUY WATCHLIST{reset}",
        f"Status: {status} | Updated: {updated} | Pending scans: {pending}",
        "Ranked model signals only; no profit guarantee and no automatic purchase.",
        "",
    ]
    if not candidates:
        lines.append(
            f"{dim}Waiting for a coin to pass the CORE or MOONSHOT filters...{reset}"
        )
        return "\n".join(lines)

    for index, raw in enumerate(candidates):
        if not isinstance(raw, dict):
            continue
        prefix = f"\033[38;5;{palette[index % len(palette)]}m" if color else ""
        liquidity_raw = raw.get("liquidity_usd")
        liquidity = (
            f"${float(liquidity_raw):,.0f}"
            if liquidity_raw is not None
            else "unknown"
        )
        chain = str(raw.get("chain") or "solana")
        chain_label = CHAIN_LABELS.get(chain, chain.upper()[:5])
        currency = str(raw.get("price_currency") or "SOL")
        price = float(raw.get("price") or raw.get("price_sol") or 0)
        price_prefix = "$" if currency == "USD" else ""
        address_label = "mint" if chain == "solana" else "contract"
        detail_lines = [
            (
                f"{prefix}#{int(raw.get('rank') or index + 1):02d} "
                f"{(raw.get('symbol') or 'UNKNOWN')!s:<12} "
                f"chain={chain_label:<3} "
                f"tier={(raw.get('tier') or 'UNKNOWN')!s:<8} "
                f"signal={int(raw.get('signal_score') or 0):3d}{reset}"
            ),
            (
                f"{prefix}    rise={float(raw.get('rise_pct') or 0):+8.2f}% "
                f"m5={float(raw.get('price_change_m5_pct') or 0):+8.2f}% "
                f"liquidity={liquidity} "
                f"price={price_prefix}{price:.12g}{reset}"
            ),
            (
                f"{prefix}    {address_label}="
                f"{(raw.get('mint') or '')!s}{reset}"
            ),
        ]
        fomo_url = str(raw.get("fomo_url") or "")
        if fomo_url:
            detail_lines.append(f"{prefix}    fomo={fomo_url}{reset}")
        market_url = str(raw.get("market_url") or "")
        if market_url:
            detail_lines.append(f"{prefix}    market={market_url}{reset}")
        detail_lines.append("")
        lines.extend(
            detail_lines
        )
    return "\n".join(lines).rstrip()
