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
    "bsc": "BNB",
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
    initial_liquidity_usd: float | None
    volume_m5_usd: float
    initial_volume_m5_usd: float
    buys_m5: int
    sells_m5: int
    price_change_m5_pct: float | None
    buy_sell_ratio: float
    observed_at: float
    updated_at: float
    decision: str = "WATCH"
    decision_reason: str = "waiting for confirmation"
    entry_zone_low: float | None = None
    entry_zone_high: float | None = None
    peak_price: float = 0.0

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

    @property
    def decision_priority(self) -> int:
        return {
            "BUY ZONE": 4,
            "BUY NOW": 3,
            "PULLBACK STARTED": 2,
            "WAIT FOR PULLBACK": 2,
            "WATCH": 1,
            "AVOID": 0,
        }.get(self.decision, 0)

    @property
    def sell_pressure_ratio(self) -> float:
        return self.sells_m5 / max(1, self.buys_m5)

    @property
    def pullback_needed_pct(self) -> tuple[float, float] | None:
        if (
            self.entry_zone_low is None
            or self.entry_zone_high is None
            or self.current_price <= 0
        ):
            return None
        shallow = max(
            0.0,
            (self.current_price - self.entry_zone_high)
            / self.current_price
            * 100,
        )
        deep = max(
            0.0,
            (self.current_price - self.entry_zone_low)
            / self.current_price
            * 100,
        )
        return shallow, deep

    @property
    def pullback_from_peak_pct(self) -> float:
        if self.peak_price <= 0 or self.current_price >= self.peak_price:
            return 0.0
        return (self.peak_price - self.current_price) / self.peak_price * 100.0

    @property
    def momentum_label(self) -> str:
        if self.price_change_m5_pct is None:
            return "UNKNOWN"
        if self.price_change_m5_pct >= 8:
            return "STRONG"
        if self.price_change_m5_pct >= 2:
            return "RISING"
        if self.price_change_m5_pct >= -2:
            return "STABLE"
        return "FALLING"

    @property
    def liquidity_label(self) -> str:
        liquidity = self.liquidity_usd or 0.0
        if liquidity >= 50_000:
            return "GOOD"
        if liquidity >= 20_000:
            return "MODERATE"
        return "THIN"

    @property
    def volume_label(self) -> str:
        if self.initial_volume_m5_usd <= 0:
            return "UNKNOWN"
        ratio = self.volume_m5_usd / self.initial_volume_m5_usd
        if ratio >= 1.15:
            return "RISING"
        if ratio <= 0.75:
            return "FALLING"
        return "STEADY"

    @property
    def risk_label(self) -> str:
        if self.decision == "AVOID" or self.tier == "MOONSHOT":
            return "HIGH"
        if (self.liquidity_usd or 0) < 20_000:
            return "HIGH"
        if abs(self.price_change_m5_pct or 0) > 30:
            return "ELEVATED"
        return "MEDIUM"


class RecommendationBook:
    """In-memory shortlist of intelligence-qualified paper candidates."""

    def __init__(
        self,
        *,
        pool_size: int = 30,
        ttl_seconds: float = 1800,
        pullback_trigger_pct: float = 8.0,
        pullback_zone_min_pct: float = 4.0,
        pullback_zone_max_pct: float = 6.0,
        pullback_started_pct: float = 2.0,
        buy_now_min_ratio: float = 1.2,
        avoid_momentum_pct: float = -8.0,
        avoid_sell_pressure_ratio: float = 2.0,
        min_liquidity_usd: float = 5_000.0,
    ) -> None:
        self.pool_size = pool_size
        self.ttl_seconds = ttl_seconds
        self.pullback_trigger_pct = pullback_trigger_pct
        self.pullback_zone_min_pct = pullback_zone_min_pct
        self.pullback_zone_max_pct = pullback_zone_max_pct
        self.pullback_started_pct = pullback_started_pct
        self.buy_now_min_ratio = buy_now_min_ratio
        self.avoid_momentum_pct = avoid_momentum_pct
        self.avoid_sell_pressure_ratio = avoid_sell_pressure_ratio
        self.min_liquidity_usd = min_liquidity_usd
        self.candidates: dict[str, RecommendationCandidate] = {}
        self._buy_zone_alerts: set[str] = set()
        self._pullback_alerts: set[str] = set()

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
            initial_liquidity_usd=quote.liquidity_usd,
            volume_m5_usd=quote.volume_m5_usd,
            initial_volume_m5_usd=quote.volume_m5_usd,
            buys_m5=quote.buys_m5,
            sells_m5=quote.sells_m5,
            price_change_m5_pct=quote.price_change_m5_pct,
            buy_sell_ratio=quote.buy_sell_ratio,
            observed_at=timestamp,
            updated_at=timestamp,
            peak_price=price,
        )
        self._refresh_decision(candidate)
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
        candidate.peak_price = max(candidate.peak_price, price)
        candidate.liquidity_usd = quote.liquidity_usd
        candidate.volume_m5_usd = quote.volume_m5_usd
        candidate.buys_m5 = quote.buys_m5
        candidate.sells_m5 = quote.sells_m5
        candidate.price_change_m5_pct = quote.price_change_m5_pct
        candidate.buy_sell_ratio = quote.buy_sell_ratio
        candidate.updated_at = time.monotonic() if now is None else now
        previous_decision = candidate.decision
        self._refresh_decision(candidate)
        if (
            candidate.decision == "BUY ZONE"
            and previous_decision != "BUY ZONE"
        ):
            self._buy_zone_alerts.add(candidate.key)
        if (
            candidate.decision == "PULLBACK STARTED"
            and previous_decision != "PULLBACK STARTED"
        ):
            self._pullback_alerts.add(candidate.key)
        return candidate

    def pop_buy_zone_alerts(self) -> list[RecommendationCandidate]:
        alerts = [
            self.candidates[key]
            for key in self._buy_zone_alerts
            if key in self.candidates
        ]
        self._buy_zone_alerts.clear()
        return sorted(alerts, key=lambda item: item.signal_score, reverse=True)

    def pop_pullback_alerts(self) -> list[RecommendationCandidate]:
        alerts = [
            self.candidates[key]
            for key in self._pullback_alerts
            if key in self.candidates
        ]
        self._pullback_alerts.clear()
        return sorted(alerts, key=lambda item: item.signal_score, reverse=True)

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
                item.decision_priority,
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

    def _refresh_decision(self, candidate: RecommendationCandidate) -> None:
        liquidity = candidate.liquidity_usd or 0.0
        initial_liquidity = candidate.initial_liquidity_usd or liquidity
        change = candidate.price_change_m5_pct
        severe_selloff = (
            change is not None
            and change <= self.avoid_momentum_pct
            and candidate.sell_pressure_ratio
            >= self.avoid_sell_pressure_ratio
        )
        liquidity_failure = liquidity < self.min_liquidity_usd
        liquidity_collapse = (
            initial_liquidity > 0 and liquidity < initial_liquidity * 0.65
        )
        if liquidity_failure or liquidity_collapse or severe_selloff:
            candidate.decision = "AVOID"
            if liquidity_failure:
                candidate.decision_reason = "liquidity below the safety floor"
            elif liquidity_collapse:
                candidate.decision_reason = "liquidity fell more than 35%"
            else:
                candidate.decision_reason = "falling price with heavy selling"
            return

        zone_low = candidate.entry_zone_low
        zone_high = candidate.entry_zone_high
        if zone_low is not None and zone_high is not None:
            if candidate.current_price > zone_high:
                if (
                    candidate.pullback_from_peak_pct
                    >= self.pullback_started_pct
                ):
                    candidate.decision = "PULLBACK STARTED"
                    candidate.decision_reason = (
                        "price is retreating from its tracked peak toward the "
                        "entry zone"
                    )
                else:
                    candidate.decision = "WAIT FOR PULLBACK"
                    candidate.decision_reason = (
                        "price remains above anchored entry zone"
                    )
                return
            if zone_low <= candidate.current_price <= zone_high:
                if (
                    change is not None
                    and change >= 0
                    and candidate.buy_sell_ratio >= 1.0
                ):
                    candidate.decision = "BUY ZONE"
                    candidate.decision_reason = (
                        "price entered the zone with recovery confirmation"
                    )
                else:
                    candidate.decision = "WATCH"
                    candidate.decision_reason = "in range, but recovery is unconfirmed"
                return
            candidate.decision = "WATCH"
            candidate.decision_reason = "price fell through the entry zone"
            return

        overextended = (
            candidate.rise_pct >= self.pullback_trigger_pct
            or (
                change is not None
                and change >= self.pullback_trigger_pct
            )
        )
        if overextended:
            candidate.entry_zone_low = candidate.current_price * (
                1 - self.pullback_zone_max_pct / 100
            )
            candidate.entry_zone_high = candidate.current_price * (
                1 - self.pullback_zone_min_pct / 100
            )
            candidate.peak_price = max(
                candidate.peak_price, candidate.current_price
            )
            candidate.decision = "WAIT FOR PULLBACK"
            candidate.decision_reason = "momentum is strong but price is extended"
            return

        if (
            change is not None
            and change >= 0
            and candidate.buy_sell_ratio >= self.buy_now_min_ratio
        ):
            candidate.decision = "BUY NOW"
            candidate.decision_reason = "qualified setup without an extended move"
            return

        candidate.decision = "WATCH"
        candidate.decision_reason = "waiting for price and buyer confirmation"


def format_recommendations(
    candidates: list[RecommendationCandidate], *, color: bool = True
) -> str:
    gold = "\033[38;5;220m" if color else ""
    reset = "\033[0m" if color else ""
    lines = [
        (
            f"{gold}DECISION-SUPPORT WATCHLIST 1-{len(candidates)} "
            "(read-only model signals; you decide manually)"
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
            f"decision={item.decision:<17} score={item.signal_score:3d} "
            f"rise={item.rise_pct:+7.2f}% "
            f"m5={m5_change:+7.2f}% liquidity={liquidity} "
            f"price={price_prefix}{item.current_price:.12g} "
            f"{address_label}={item.mint}"
        )
        if item.entry_zone_low is not None and item.entry_zone_high is not None:
            pullback = item.pullback_needed_pct or (0.0, 0.0)
            lines.append(
                f"    entry={price_prefix}{item.entry_zone_low:.12g}-"
                f"{price_prefix}{item.entry_zone_high:.12g} "
                f"from_peak={item.pullback_from_peak_pct:.1f}% "
                f"pullback={pullback[0]:.1f}%-{pullback[1]:.1f}% "
                f"reason={item.decision_reason}"
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
    alerts: list[RecommendationCandidate] | None = None,
) -> dict[str, Any]:
    return {
        "generated_at": time.time(),
        "pending_count": pending_count,
        "poll_seconds": poll_seconds,
        "alerts": [
            {
                "symbol": candidate.symbol,
                "chain": candidate.chain,
                "price": candidate.current_price,
                "price_currency": candidate.price_currency,
                "entry_zone_low": candidate.entry_zone_low,
                "entry_zone_high": candidate.entry_zone_high,
                "decision": candidate.decision,
                "pullback_from_peak_pct": candidate.pullback_from_peak_pct,
            }
            for candidate in (alerts or [])
        ],
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
                "decision": candidate.decision,
                "decision_reason": candidate.decision_reason,
                "entry_zone_low": candidate.entry_zone_low,
                "entry_zone_high": candidate.entry_zone_high,
                "pullback_needed_pct": candidate.pullback_needed_pct,
                "pullback_from_peak_pct": candidate.pullback_from_peak_pct,
                "momentum_label": candidate.momentum_label,
                "liquidity_label": candidate.liquidity_label,
                "volume_label": candidate.volume_label,
                "risk_label": candidate.risk_label,
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
    raw_alerts = snapshot.get("alerts")
    alerts = raw_alerts if isinstance(raw_alerts, list) else []

    status = "STALE — scanner is not updating" if stale else "LIVE"
    lines = [
        f"{bold}LAUNCH GUARD — READ-ONLY DECISION SUPPORT{reset}",
        f"Status: {status} | Updated: {updated} | Pending scans: {pending}",
        "DISCOVER → SCORE → WAIT / BUY ZONE → ALERT → YOU BUY MANUALLY",
        "Model signals only; no profit guarantee and no automatic purchase.",
        "",
    ]
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        currency = str(alert.get("price_currency") or "SOL")
        price_prefix = "$" if currency == "USD" else ""
        chain = str(alert.get("chain") or "solana")
        chain_label = CHAIN_LABELS.get(chain, chain.upper()[:5])
        decision = str(alert.get("decision") or "BUY ZONE")
        lines.append(
            f"{bold}>>> {decision} ALERT: {alert.get('symbol') or 'UNKNOWN'} "
            f"[{chain_label}] current={price_prefix}"
            f"{float(alert.get('price') or 0):.12g}{reset}"
        )
    if alerts:
        lines.append("")
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
                f"{prefix}    decision={(raw.get('decision') or 'WATCH')!s} "
                f"| risk={(raw.get('risk_label') or 'HIGH')!s}{reset}"
            ),
            (
                f"{prefix}    rise={float(raw.get('rise_pct') or 0):+8.2f}% "
                f"m5={float(raw.get('price_change_m5_pct') or 0):+8.2f}% "
                f"liquidity={liquidity} "
                f"price={price_prefix}{price:.12g}{reset}"
            ),
            (
                f"{prefix}    momentum="
                f"{(raw.get('momentum_label') or 'UNKNOWN')!s} | liquidity="
                f"{(raw.get('liquidity_label') or 'UNKNOWN')!s} | volume="
                f"{(raw.get('volume_label') or 'UNKNOWN')!s}{reset}"
            ),
            (
                f"{prefix}    reason="
                f"{(raw.get('decision_reason') or '')!s}{reset}"
            ),
            (
                f"{prefix}    {address_label}="
                f"{(raw.get('mint') or '')!s}{reset}"
            ),
        ]
        zone_low = raw.get("entry_zone_low")
        zone_high = raw.get("entry_zone_high")
        if zone_low is not None and zone_high is not None:
            pullback_raw = raw.get("pullback_needed_pct")
            pullback = (
                pullback_raw
                if isinstance(pullback_raw, (list, tuple))
                and len(pullback_raw) == 2
                else (0.0, 0.0)
            )
            detail_lines.insert(
                4,
                (
                    f"{prefix}    preferred entry="
                    f"{price_prefix}{float(zone_low):.12g} - "
                    f"{price_prefix}{float(zone_high):.12g} | pullback needed="
                    f"{float(pullback[0]):.1f}%-{float(pullback[1]):.1f}%"
                    f"{reset}"
                ),
            )
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
