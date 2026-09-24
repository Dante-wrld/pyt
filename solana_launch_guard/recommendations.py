from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .intelligence import IntelligenceResult
from .market import MarketQuote

ACTIONABLE_BUY_DECISIONS = frozenset({"MOMENTUM BUY", "BUY ZONE", "BUY NOW", "EARLY BUY"})

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
    pair_address: str | None = None
    decision: str = "WATCH"
    decision_reason: str = "waiting for confirmation"
    entry_zone_low: float | None = None
    entry_zone_high: float | None = None
    pullback_low_price: float | None = None
    peak_price: float = 0.0
    entry_confirmation_count: int = 0
    entry_confirmation_signal: str = ""
    entry_confirmation_required: int = 1
    planned_entry_price: float | None = None
    planned_stop_pct: float = 20.0
    planned_target_pct: float = 40.0

    def to_json(self) -> str:
        """Serialize the complete signal state for restart-safe monitoring."""
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, payload: str) -> RecommendationCandidate:
        values = json.loads(payload)
        if not isinstance(values, dict):
            raise TypeError("candidate snapshot must be a JSON object")
        return cls(**values)

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
            "MOMENTUM BUY": 4,
            "BUY NOW": 3,
            "EARLY BUY": 3,
            "ENTRY PENDING": 2,
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
    def planned_stop_price(self) -> float | None:
        if self.planned_entry_price is None:
            return None
        return self.planned_entry_price * (1 - self.planned_stop_pct / 100.0)

    @property
    def planned_target_price(self) -> float | None:
        if self.planned_entry_price is None:
            return None
        return self.planned_entry_price * (1 + self.planned_target_pct / 100.0)

    @property
    def planned_reward_risk_ratio(self) -> float:
        if self.planned_stop_pct <= 0:
            return 0.0
        return self.planned_target_pct / self.planned_stop_pct

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
        pullback_reclaim_pct: float = 2.0,
        entry_confirmation_polls: int = 3,
        # A momentum candidate requiring the same strong-momentum reading to
        # persist for entry_confirmation_polls consecutive polls (~30-45s)
        # tends to select for local tops - by the time it's confirmed 3
        # times in a row, the move is often already exhausting (observed
        # live: BETBOLT, WETCAT, and WHT all bought within seconds of a
        # local peak, then reversed hard). MOMENTUM BUY's own evidence bar
        # (50+ trades, 1.5x ratio, rising volume) is already substantial on
        # a single poll, so it acts on the first sighting instead of
        # requiring it to repeat.
        momentum_buy_confirmation_polls: int = 1,
        entry_min_signal_score: int = 65,
        entry_min_liquidity_retention_pct: float = 80.0,
        entry_require_nonfalling_volume: bool = True,
        core_stop_loss_pct: float = 20.0,
        core_take_profit_pct: float = 30.0,
        moonshot_stop_loss_pct: float = 40.0,
        moonshot_take_profit_pct: float = 5_000.0,
        min_entry_reward_risk_ratio: float = 2.0,
        buy_now_min_ratio: float = 1.2,
        momentum_buy_min_ratio: float = 1.5,
        momentum_buy_min_trades: int = 50,
        momentum_buy_min_liquidity_growth_pct: float = 20.0,
        avoid_momentum_pct: float = -8.0,
        avoid_sell_pressure_ratio: float = 2.0,
        min_liquidity_usd: float = 5_000.0,
        early_buy_max_deviation_pct: float = 8.0,
        early_buy_min_ratio: float = 1.1,
        early_buy_min_trades: int = 15,
        early_buy_min_liquidity_usd: float = 15_000.0,
    ) -> None:
        self.pool_size = pool_size
        self.ttl_seconds = ttl_seconds
        self.pullback_trigger_pct = pullback_trigger_pct
        self.pullback_zone_min_pct = pullback_zone_min_pct
        self.pullback_zone_max_pct = pullback_zone_max_pct
        self.pullback_started_pct = pullback_started_pct
        self.pullback_reclaim_pct = pullback_reclaim_pct
        self.entry_confirmation_polls = entry_confirmation_polls
        self.momentum_buy_confirmation_polls = momentum_buy_confirmation_polls
        self.entry_min_signal_score = entry_min_signal_score
        self.entry_min_liquidity_retention_pct = (
            entry_min_liquidity_retention_pct
        )
        self.entry_require_nonfalling_volume = entry_require_nonfalling_volume
        self.core_stop_loss_pct = core_stop_loss_pct
        self.core_take_profit_pct = core_take_profit_pct
        self.moonshot_stop_loss_pct = moonshot_stop_loss_pct
        self.moonshot_take_profit_pct = moonshot_take_profit_pct
        self.min_entry_reward_risk_ratio = min_entry_reward_risk_ratio
        self.buy_now_min_ratio = buy_now_min_ratio
        self.momentum_buy_min_ratio = momentum_buy_min_ratio
        self.momentum_buy_min_trades = momentum_buy_min_trades
        self.momentum_buy_min_liquidity_growth_pct = momentum_buy_min_liquidity_growth_pct
        self.avoid_momentum_pct = avoid_momentum_pct
        self.avoid_sell_pressure_ratio = avoid_sell_pressure_ratio
        self.min_liquidity_usd = min_liquidity_usd
        self.early_buy_max_deviation_pct = early_buy_max_deviation_pct
        self.early_buy_min_ratio = early_buy_min_ratio
        self.early_buy_min_trades = early_buy_min_trades
        self.early_buy_min_liquidity_usd = early_buy_min_liquidity_usd
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
        timestamp = time.time() if now is None else now
        existing = self.candidates.get(quote.recommendation_key)
        if existing is not None:
            self.update(quote, now=timestamp)
            return existing

        stop_pct = (
            self.moonshot_stop_loss_pct
            if result.tier == "MOONSHOT"
            else self.core_stop_loss_pct
        )
        configured_target_pct = (
            self.moonshot_take_profit_pct
            if result.tier == "MOONSHOT"
            else self.core_take_profit_pct
        )
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
            pair_address=quote.pair_address or None,
            peak_price=price,
            entry_confirmation_required=self.entry_confirmation_polls,
            planned_stop_pct=stop_pct,
            planned_target_pct=max(
                configured_target_pct,
                stop_pct * self.min_entry_reward_risk_ratio,
            ),
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
        candidate.pair_address = quote.pair_address or None
        candidate.liquidity_usd = quote.liquidity_usd
        candidate.volume_m5_usd = quote.volume_m5_usd
        candidate.buys_m5 = quote.buys_m5
        candidate.sells_m5 = quote.sells_m5
        candidate.price_change_m5_pct = quote.price_change_m5_pct
        candidate.buy_sell_ratio = quote.buy_sell_ratio
        candidate.updated_at = time.time() if now is None else now
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
        timestamp = time.time() if now is None else now
        expired = [
            key
            for key, candidate in self.candidates.items()
            if timestamp - candidate.updated_at > self.ttl_seconds
        ]
        for key in expired:
            del self.candidates[key]

    def restore(self, candidate: RecommendationCandidate) -> bool:
        """Restore persisted state without making a stale signal look fresh."""
        if (
            not candidate.mint
            or not candidate.chain
            or candidate.initial_price <= 0
            or candidate.current_price <= 0
        ):
            return False
        self.candidates[candidate.key] = candidate
        self._trim()
        return candidate.key in self.candidates

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

    def ranked_for_execution(self, limit: int = 10) -> list[RecommendationCandidate]:
        """Like ranked(), but for the snapshot a live trial reads for buy
        decisions rather than for display. recommendation_limit exists to
        keep a human-facing dashboard readable, not to gate what a trial
        is allowed to act on - a genuinely buy-worthy candidate (MOMENTUM
        BUY, BUY ZONE, BUY NOW, EARLY BUY) can be pushed out of the top N
        by unrelated higher-scoring candidates and, for EARLY BUY
        specifically, its low rise_pct is *the point* of the signal, not a
        reason to rank it last. Union in any actionable candidate the
        ranked cut dropped so it's never silently invisible to the trial.
        """
        top = self.ranked(limit)
        seen = {c.key for c in top}
        overflow = [
            c for c in self.candidates.values()
            if c.decision in ACTIONABLE_BUY_DECISIONS and c.key not in seen
        ]
        return top + overflow

    def _trim(self) -> None:
        if len(self.candidates) <= self.pool_size:
            return
        lowest = min(
            self.candidates.values(),
            key=lambda item: (item.signal_score, item.observed_at),
        )
        del self.candidates[lowest.key]

    @staticmethod
    def _reset_entry_confirmation(candidate: RecommendationCandidate) -> None:
        candidate.entry_confirmation_count = 0
        candidate.entry_confirmation_signal = ""

    def _set_non_entry(
        self,
        candidate: RecommendationCandidate,
        decision: str,
        reason: str,
    ) -> None:
        self._reset_entry_confirmation(candidate)
        candidate.decision = decision
        candidate.decision_reason = reason

    def _entry_block_reason(
        self, candidate: RecommendationCandidate
    ) -> str | None:
        if candidate.signal_score < self.entry_min_signal_score:
            return (
                f"signal score {candidate.signal_score} is below entry minimum "
                f"{self.entry_min_signal_score}"
            )
        initial_liquidity = candidate.initial_liquidity_usd or 0.0
        current_liquidity = candidate.liquidity_usd or 0.0
        if initial_liquidity > 0:
            retained_pct = current_liquidity / initial_liquidity * 100.0
            if retained_pct < self.entry_min_liquidity_retention_pct:
                return (
                    f"liquidity retention {retained_pct:.1f}% is below entry "
                    f"minimum {self.entry_min_liquidity_retention_pct:.1f}%"
                )
        if (
            self.entry_require_nonfalling_volume
            and candidate.volume_label == "FALLING"
        ):
            return "five-minute volume is falling"
        return None

    def _propose_entry(
        self,
        candidate: RecommendationCandidate,
        decision: str,
        reason: str,
        *,
        confirmation_polls: int | None = None,
    ) -> None:
        required_polls = self.entry_confirmation_polls if confirmation_polls is None else confirmation_polls
        blocked = self._entry_block_reason(candidate)
        if blocked is not None:
            self._set_non_entry(
                candidate,
                "WATCH",
                f"entry blocked: {blocked}",
            )
            return
        if candidate.entry_confirmation_signal == decision:
            candidate.entry_confirmation_count += 1
        else:
            candidate.entry_confirmation_signal = decision
            candidate.entry_confirmation_count = 1
        candidate.entry_confirmation_required = required_polls
        if candidate.entry_confirmation_count < required_polls:
            candidate.decision = "ENTRY PENDING"
            candidate.decision_reason = (
                f"{decision} confirmation "
                f"{candidate.entry_confirmation_count}/"
                f"{required_polls}: {reason}"
            )
            return
        candidate.decision = decision
        if candidate.entry_confirmation_count == required_polls:
            candidate.planned_entry_price = candidate.current_price
        candidate.decision_reason = (
            f"confirmed for {candidate.entry_confirmation_count} consecutive "
            f"checks: {reason}"
        )

    def _momentum_buy_reason(
        self, candidate: RecommendationCandidate, *, require_rising_volume: bool
    ) -> str | None:
        """A strong move with firm buy pressure and (depending on context)
        non-falling or actively rising volume - the one setup the
        pullback-only path can never reach, since it requires a real pullback
        to exist first. Deliberately stricter than the pullback path's own
        bar (buy_now_min_ratio): there is no dip-and-reclaim confirmation to
        lean on here, so this function's job is finding *some* real evidence
        of firm buy pressure, since there is no other confirmation to lean on.

        require_rising_volume=True is for a candidate on its very first
        overextended observation: initial_volume_m5_usd is bootstrapped from
        that same observation, so volume_label is trivially "STEADY" (ratio
        exactly 1.0) regardless of the token's real behavior - not real
        evidence yet, so only a genuinely rising reading counts. A candidate
        that already has an anchored zone has necessarily survived at least
        one full overextend-and-anchor cycle already, so its STEADY reading
        reflects real, accumulated history and is accepted too.

        Buy pressure is confirmed by EITHER a strong buy/sell transaction
        ratio OR strong liquidity growth since the candidate's first
        observation - a high-frequency, near-even transaction count can
        still mask a genuine pump if buyers are moving meaningfully more
        size than sellers, and liquidity growth is direct evidence of real
        new capital (not just churn between existing holders) that a count
        ratio alone can't see. DexScreener doesn't expose buy vs sell dollar
        volume separately, so this is the closest available proxy.
        """
        if candidate.momentum_label not in {"RISING", "STRONG"}:
            return None
        required_volume = {"RISING"} if require_rising_volume else {"STEADY", "RISING"}
        if candidate.volume_label not in required_volume:
            return None
        # A ratio alone can't distinguish "1,700 real trades, mostly buys"
        # from "13 trades, mostly buys" - the second is noise, not evidence.
        if candidate.buys_m5 + candidate.sells_m5 < self.momentum_buy_min_trades:
            return None
        ratio_confirmed = candidate.buy_sell_ratio >= self.momentum_buy_min_ratio
        liquidity_growth_pct = None
        initial_liquidity = candidate.initial_liquidity_usd
        if initial_liquidity and initial_liquidity > 0 and candidate.liquidity_usd is not None:
            liquidity_growth_pct = (candidate.liquidity_usd / initial_liquidity - 1) * 100
        liquidity_confirmed = (
            liquidity_growth_pct is not None
            and liquidity_growth_pct >= self.momentum_buy_min_liquidity_growth_pct
        )
        if not (ratio_confirmed or liquidity_confirmed):
            return None
        evidence = (
            f"a {candidate.buy_sell_ratio:.2f} buy/sell ratio" if ratio_confirmed
            else f"liquidity up {liquidity_growth_pct:.0f}% since first seen"
        )
        return (
            f"momentum continuation: {candidate.momentum_label.lower()} price "
            f"action with {candidate.volume_label.lower()} volume, "
            f"{candidate.buys_m5 + candidate.sells_m5} five-minute trades, "
            f"and {evidence}"
        )

    def _early_buy_reason(self, candidate: RecommendationCandidate) -> str | None:
        """A deliberately weaker bar than momentum continuation - the trade
        here is accepting a token with no proven move yet in exchange for a
        price still close to where we first saw it, rather than waiting for
        either a full overextend-and-pullback cycle or a momentum
        confirmation to complete. Only reachable while price is still near
        initial_price (see the caller): this only judges whether there is
        enough real activity to believe the pool isn't dead or a decoy, not
        whether a trend already exists.

        Being close to the starting price is not itself a reason to buy - a
        token actively declining in the last five minutes must not qualify
        just because it hasn't fallen far enough yet to leave the window.
        Flat/unknown (no five-minute data yet, the genuinely earliest case)
        and mild upticks still pass; only active decline is excluded.
        """
        if abs(candidate.rise_pct) > self.early_buy_max_deviation_pct:
            return None
        if candidate.momentum_label == "FALLING":
            return None
        if (candidate.liquidity_usd or 0) < self.early_buy_min_liquidity_usd:
            return None
        if candidate.buys_m5 + candidate.sells_m5 < self.early_buy_min_trades:
            return None
        if candidate.buy_sell_ratio < self.early_buy_min_ratio:
            return None
        return (
            f"still within {self.early_buy_max_deviation_pct:.0f}% of its starting "
            f"price ({candidate.rise_pct:+.1f}%) with {candidate.buys_m5 + candidate.sells_m5} "
            f"five-minute trades and a {candidate.buy_sell_ratio:.2f} buy/sell ratio"
        )

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
            if liquidity_failure:
                reason = "liquidity below the safety floor"
            elif liquidity_collapse:
                reason = "liquidity fell more than 35%"
            else:
                reason = "falling price with heavy selling"
            self._set_non_entry(candidate, "AVOID", reason)
            return

        zone_low = candidate.entry_zone_low
        zone_high = candidate.entry_zone_high
        if zone_low is not None and zone_high is not None:
            if candidate.current_price > zone_high:
                momentum_reason = self._momentum_buy_reason(
                    candidate, require_rising_volume=False
                )
                if momentum_reason is not None:
                    self._propose_entry(candidate, "MOMENTUM BUY", momentum_reason,
                                        confirmation_polls=self.momentum_buy_confirmation_polls)
                    return
                # A token that keeps making new highs needs its zone to
                # follow: re-anchor once price has run far enough past the
                # existing zone to represent a fresh overextension. Without
                # this, a real winner's zone stays frozen at its first pump
                # and it can never again earn a confirmed-pullback entry
                # unless it collapses all the way back to that stale level.
                if candidate.current_price >= zone_high * (
                    1 + self.pullback_trigger_pct / 100
                ):
                    candidate.entry_zone_low = candidate.current_price * (
                        1 - self.pullback_zone_max_pct / 100
                    )
                    candidate.entry_zone_high = candidate.current_price * (
                        1 - self.pullback_zone_min_pct / 100
                    )
                    candidate.peak_price = max(
                        candidate.peak_price, candidate.current_price
                    )
                    candidate.pullback_low_price = None
                    self._set_non_entry(
                        candidate,
                        "WAIT FOR PULLBACK",
                        "new peak reached; entry zone re-anchored",
                    )
                    return
                # Not currently in a pullback episode; any previously tracked
                # low belongs to a different dip and must not count toward a
                # future reclaim.
                candidate.pullback_low_price = None
                if (
                    candidate.pullback_from_peak_pct
                    >= self.pullback_started_pct
                ):
                    self._set_non_entry(
                        candidate,
                        "PULLBACK STARTED",
                        "price is retreating from its tracked peak toward the "
                        "entry zone",
                    )
                else:
                    self._set_non_entry(
                        candidate,
                        "WAIT FOR PULLBACK",
                        "price remains above anchored entry zone",
                    )
                return
            # Require a genuine higher low, not just a single favorable poll:
            # compare against the lowest price seen so far this pullback
            # episode, then fold the current price into that tracked low.
            tracked_low = candidate.pullback_low_price
            reclaim_pct = (
                (candidate.current_price / tracked_low - 1) * 100
                if tracked_low
                else 0.0
            )
            candidate.pullback_low_price = min(
                tracked_low or candidate.current_price, candidate.current_price
            )
            if zone_low <= candidate.current_price <= zone_high:
                blocked = self._entry_block_reason(candidate)
                if blocked is not None:
                    self._set_non_entry(
                        candidate, "WATCH", f"entry blocked: {blocked}"
                    )
                elif (
                    reclaim_pct >= self.pullback_reclaim_pct
                    and change is not None
                    and change >= 0
                    and candidate.buy_sell_ratio >= self.buy_now_min_ratio
                ):
                    self._propose_entry(
                        candidate,
                        "BUY ZONE",
                        f"price reclaimed {reclaim_pct:.1f}% off the pullback "
                        "low with recovery confirmation",
                    )
                else:
                    self._set_non_entry(
                        candidate,
                        "WATCH",
                        "in range, but recovery is unconfirmed",
                    )
                return
            self._set_non_entry(
                candidate, "WATCH", "price fell through the entry zone"
            )
            return

        overextended = (
            candidate.rise_pct >= self.pullback_trigger_pct
            or (
                change is not None
                and change >= self.pullback_trigger_pct
            )
        )
        if overextended:
            momentum_reason = self._momentum_buy_reason(
                candidate, require_rising_volume=True
            )
            if momentum_reason is not None:
                self._propose_entry(candidate, "MOMENTUM BUY", momentum_reason,
                                    confirmation_polls=self.momentum_buy_confirmation_polls)
                return
            candidate.entry_zone_low = candidate.current_price * (
                1 - self.pullback_zone_max_pct / 100
            )
            candidate.entry_zone_high = candidate.current_price * (
                1 - self.pullback_zone_min_pct / 100
            )
            candidate.peak_price = max(
                candidate.peak_price, candidate.current_price
            )
            self._set_non_entry(
                candidate,
                "WAIT FOR PULLBACK",
                "momentum is strong but price is extended",
            )
            return

        early_buy_reason = self._early_buy_reason(candidate)
        if early_buy_reason is not None:
            self._propose_entry(candidate, "EARLY BUY", early_buy_reason)
            return

        if (
            change is not None
            and change >= 0
            and candidate.buy_sell_ratio >= self.buy_now_min_ratio
        ):
            self._propose_entry(
                candidate,
                "BUY NOW",
                "qualified setup without an extended move",
            )
            return

        self._set_non_entry(
            candidate, "WATCH", "waiting for price and buyer confirmation"
        )


def format_recommendations(
    candidates: list[RecommendationCandidate], *, color: bool = True
) -> str:
    gold = "\033[38;5;220m" if color else ""
    reset = "\033[0m" if color else ""
    lines = [
        (
            f"{gold}DECISION-SUPPORT WATCHLIST 1-{len(candidates)} "
            "(model signals; orders require a separate mint allow-list)"
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
        lines.append(
            f"    confirmation={item.entry_confirmation_count}/"
            f"{item.entry_confirmation_required} "
            f"reason={item.decision_reason}"
        )
        if item.planned_entry_price is not None:
            lines.append(
                f"    paper plan entry={price_prefix}"
                f"{item.planned_entry_price:.12g} stop={price_prefix}"
                f"{(item.planned_stop_price or 0):.12g} target={price_prefix}"
                f"{(item.planned_target_price or 0):.12g} "
                f"reward/risk={item.planned_reward_risk_ratio:.2f}"
            )
        if item.entry_zone_low is not None and item.entry_zone_high is not None:
            pullback = item.pullback_needed_pct or (0.0, 0.0)
            lines.append(
                f"    entry={price_prefix}{item.entry_zone_low:.12g}-"
                f"{price_prefix}{item.entry_zone_high:.12g} "
                f"from_peak={item.pullback_from_peak_pct:.1f}% "
                f"pullback={pullback[0]:.1f}%-{pullback[1]:.1f}%"
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
    tracked_candidates: list[RecommendationCandidate] | None = None,
) -> dict[str, Any]:
    snapshot = {
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
                "entry_confirmation_count": candidate.entry_confirmation_count,
                "entry_confirmation_required": (
                    candidate.entry_confirmation_required
                ),
                "planned_entry_price": candidate.planned_entry_price,
                "planned_stop_price": candidate.planned_stop_price,
                "planned_target_price": candidate.planned_target_price,
                "planned_reward_risk_ratio": (
                    candidate.planned_reward_risk_ratio
                ),
            }
            for candidate in (alerts or [])
        ],
        "candidates": [
            {
                "rank": rank,
                "quoted_at": candidate.updated_at,
                "mint": candidate.mint,
                "symbol": candidate.symbol,
                "chain": candidate.chain,
                "tier": candidate.tier,
                "signal_score": candidate.signal_score,
                "rise_pct": candidate.rise_pct,
                "price_change_m5_pct": candidate.price_change_m5_pct or 0.0,
                "liquidity_usd": candidate.liquidity_usd,
                "initial_liquidity_usd": candidate.initial_liquidity_usd,
                "peak_price": candidate.peak_price,
                "buys_m5": candidate.buys_m5,
                "sells_m5": candidate.sells_m5,
                "buy_sell_ratio": candidate.buy_sell_ratio,
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
                "entry_confirmation_count": candidate.entry_confirmation_count,
                "entry_confirmation_required": (
                    candidate.entry_confirmation_required
                ),
                "planned_entry_price": candidate.planned_entry_price,
                "planned_stop_price": candidate.planned_stop_price,
                "planned_target_price": candidate.planned_target_price,
                "planned_reward_risk_ratio": (
                    candidate.planned_reward_risk_ratio
                ),
                "fomo_url": candidate.fomo_url,
                "market_url": candidate.market_url,
            }
            for rank, candidate in enumerate(candidates, start=1)
        ],
    }
    if tracked_candidates is not None:
        snapshot["tracked_candidates"] = build_snapshot(
            tracked_candidates, pending_count=0, poll_seconds=poll_seconds,
        )["candidates"]
    return snapshot


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
        "DISCOVER → SCORE → CONFIRM → BUY ZONE → ALERT → YOU DECIDE",
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
                f"{prefix}    entry confirmation="
                f"{int(raw.get('entry_confirmation_count') or 0)}/"
                f"{int(raw.get('entry_confirmation_required') or 1)}{reset}"
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
        planned_entry = raw.get("planned_entry_price")
        planned_stop = raw.get("planned_stop_price")
        planned_target = raw.get("planned_target_price")
        if (
            planned_entry is not None
            and planned_stop is not None
            and planned_target is not None
        ):
            detail_lines.insert(
                2,
                (
                    f"{prefix}    paper plan: entry={price_prefix}"
                    f"{float(planned_entry):.12g} stop={price_prefix}"
                    f"{float(planned_stop):.12g} target={price_prefix}"
                    f"{float(planned_target):.12g} | reward/risk="
                    f"{float(raw.get('planned_reward_risk_ratio') or 0):.2f}"
                    f"{reset}"
                ),
            )
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
