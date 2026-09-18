from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

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
            if candidate.mint in seen_mints or symbol_key in seen_symbols:
                continue
            unique.append(candidate)
            seen_mints.add(candidate.mint)
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
                "tier": candidate.tier,
                "signal_score": candidate.signal_score,
                "rise_pct": candidate.rise_pct,
                "price_change_m5_pct": candidate.price_change_m5_pct or 0.0,
                "liquidity_usd": candidate.liquidity_usd,
                "price_sol": candidate.current_price_sol,
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
        datetime.fromtimestamp(generated_at).strftime("%Y-%m-%d %H:%M:%S")
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
        lines.extend(
            [
                (
                    f"{prefix}#{int(raw.get('rank') or index + 1):02d} "
                    f"{str(raw.get('symbol') or 'UNKNOWN'):<12} "
                    f"tier={str(raw.get('tier') or 'UNKNOWN'):<8} "
                    f"signal={int(raw.get('signal_score') or 0):3d}{reset}"
                ),
                (
                    f"{prefix}    rise={float(raw.get('rise_pct') or 0):+8.2f}% "
                    f"m5={float(raw.get('price_change_m5_pct') or 0):+8.2f}% "
                    f"liquidity={liquidity} "
                    f"price={float(raw.get('price_sol') or 0):.12g}{reset}"
                ),
                f"{prefix}    mint={str(raw.get('mint') or '')}{reset}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()
