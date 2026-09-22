"""Deterministic, shadow-only recovery and position reviews.

These decisions never construct or broadcast a transaction. The existing
RiskArbiter remains the final authority for an entry proposal.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _epoch(value: Any) -> float:
    """Accept either a numeric epoch (live_trial) or an ISO-8601 string
    (shadow's CapitalBook.mark_shadow_position uses datetime.isoformat())."""
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return 0.0
    return 0.0


@dataclass(frozen=True)
class ShadowRecoveryPolicy:
    pullback_pct: float = 4.0
    confirmations: int = 3
    min_liquidity_usd: float = 5_000.0
    min_liquidity_retention_pct: float = 80.0
    min_score: int = 65
    buy_sell_ratio: float = 1.2
    momentum_buy_min_ratio: float = 1.5
    momentum_buy_min_trades: int = 50
    momentum_buy_min_liquidity_growth_pct: float = 20.0
    early_buy_min_ratio: float = 1.1
    early_buy_min_trades: int = 15
    trailing_activation_pct: float = 20.0
    trailing_stop_pct: float = 12.0
    momentum_exit_pct: float = -8.0
    sell_pressure_ratio: float = 1.5
    liquidity_drop_pct: float = 30.0
    principal_multiple: float = 2.0
    half_profit_multiple: float = 3.0
    second_stage_fraction: float = 0.5
    min_sell_usd: float = 2.0
    stagnation_window_seconds: float = 300.0
    stagnation_min_gain_pct: float = 3.0
    require_medium_risk: bool = True

    @classmethod
    def from_env(cls) -> ShadowRecoveryPolicy:
        get = lambda name, default: float(os.getenv(name, str(default)))
        get_bool = lambda name, default: os.getenv(name, str(default)).strip().lower() in {"true", "1", "yes", "on"}
        policy = cls(
            pullback_pct=get("PULLBACK_ZONE_MIN_PCT", 4),
            confirmations=int(get("ENTRY_CONFIRMATION_POLLS", 3)),
            min_liquidity_usd=get("INTELLIGENCE_MIN_LIQUIDITY_USD", 5_000),
            min_liquidity_retention_pct=get("ENTRY_MIN_LIQUIDITY_RETENTION_PCT", 80),
            min_score=int(get("ENTRY_MIN_SIGNAL_SCORE", 65)),
            buy_sell_ratio=get("BUY_NOW_MIN_RATIO", 1.2),
            momentum_buy_min_ratio=get("MOMENTUM_BUY_MIN_RATIO", 1.5),
            momentum_buy_min_trades=int(get("MOMENTUM_BUY_MIN_TRADES", 50)),
            momentum_buy_min_liquidity_growth_pct=get(
                "MOMENTUM_BUY_MIN_LIQUIDITY_GROWTH_PCT", 20.0
            ),
            early_buy_min_ratio=get("EARLY_BUY_MIN_RATIO", 1.1),
            early_buy_min_trades=int(get("EARLY_BUY_MIN_TRADES", 15)),
            trailing_activation_pct=get("TRAILING_ACTIVATION_PCT", 20),
            trailing_stop_pct=get("TRAILING_STOP_PCT", 12),
            momentum_exit_pct=get("MOMENTUM_EXIT_PCT", -8),
            sell_pressure_ratio=get("SELL_PRESSURE_RATIO", 1.5),
            liquidity_drop_pct=get("LIQUIDITY_DROP_PCT", 30),
            principal_multiple=get("AUTO_SELL_PRINCIPAL_MULTIPLE", 2),
            half_profit_multiple=get("AUTO_SELL_HALF_PROFIT_MULTIPLE", 3),
            stagnation_window_seconds=get("STAGNATION_WINDOW_SECONDS", 300),
            stagnation_min_gain_pct=get("STAGNATION_MIN_GAIN_PCT", 3),
            second_stage_fraction=get("AUTO_SELL_SECOND_STAGE_FRACTION", 0.5),
            min_sell_usd=get("PORTFOLIO_MIN_SELL_VALUE_USD", 2),
            require_medium_risk=get_bool("ENTRY_REQUIRE_MEDIUM_RISK", True),
        )
        if (policy.confirmations < 3 or policy.pullback_pct <= 0
                or not 0 < policy.trailing_stop_pct < 100
                or policy.stagnation_window_seconds <= 0):
            raise ValueError("invalid shadow recovery configuration: confirmations >= 3, pullback, trailing stop and stagnation window required")
        return policy


def assess_entry(candidate: dict[str, Any], policy: ShadowRecoveryPolicy) -> dict[str, Any]:
    """Explain all missing evidence; never infer missing market data as favorable."""
    price = _number(candidate.get("price"))
    peak = _number(candidate.get("peak_price"))
    pullback = _number(candidate.get("pullback_from_peak_pct"))
    # Peak is persisted by the recommendation engine and may be absent from
    # its public snapshot; its computed pullback is the authoritative field.
    if peak > price > 0:
        pullback = max(pullback, (peak - price) / peak * 100)
    liquidity = _number(candidate.get("liquidity_usd"))
    baseline = _number(candidate.get("initial_liquidity_usd"))
    count = int(_number(candidate.get("entry_confirmation_count")))
    is_momentum_buy = candidate.get("decision") == "MOMENTUM BUY"
    # A momentum candidate's own confirmation threshold is deliberately
    # lower (see RecommendationBook.momentum_buy_confirmation_polls) - the
    # general max(3, ...) floor below exists for the pullback/BUY NOW/BUY
    # ZONE paths, which have no other timing pressure, but re-imposing it
    # here would silently erase the whole point of that lower threshold by
    # forcing hunter-v1 to wait through the same 3 polls regardless.
    required = (
        max(1, int(_number(candidate.get("entry_confirmation_required"))))
        if is_momentum_buy
        else max(3, policy.confirmations, int(_number(candidate.get("entry_confirmation_required"))))
    )
    momentum = str(candidate.get("momentum_label") or "UNKNOWN").upper()
    volume = str(candidate.get("volume_label") or "UNKNOWN").upper()
    risk = str(candidate.get("risk_label") or "UNKNOWN").upper()
    # A momentum-continuation candidate is, by definition, still extending
    # rather than pulling back - the pullback requirement below would always
    # fail it. It carries its own, stricter buy-pressure bar instead (a
    # higher buy/sell ratio) since it has no dip-and-reclaim confirmation to
    # lean on. Volume just needs to not be actively falling - same bar the
    # pullback path uses - not necessarily accelerating.
    #
    # An early-buy candidate is still close to its starting price by design
    # - it has neither a pullback to measure nor (usually) any upward
    # momentum yet, since the whole point is buying before a move has
    # happened. It carries its own, deliberately weaker buy-pressure and
    # trade-count bar in exchange for accepting that lack of evidence.
    is_early_buy = candidate.get("decision") == "EARLY BUY"
    failures = []
    codes = []
    if price <= 0:
        failures.append("no usable price")
        codes.append("pullback")
    elif not is_momentum_buy and not is_early_buy and pullback < policy.pullback_pct:
        failures.append(f"meaningful pullback missing ({pullback:.1f}%/{policy.pullback_pct:.1f}%)")
        codes.append("pullback")
    if not is_early_buy and (
        momentum not in {"RISING", "STRONG"}
        or _number(candidate.get("price_change_m5_pct"), -1) <= 0
    ):
        failures.append("short-term momentum has not turned upward")
        codes.append("momentum")
    if liquidity < policy.min_liquidity_usd or (baseline > 0 and liquidity / baseline * 100 < policy.min_liquidity_retention_pct):
        failures.append("liquidity below floor or retention requirement")
        codes.append("liquidity")
    if volume not in {"STEADY", "RISING"} or _number(candidate.get("buys_m5")) <= 0:
        failures.append("volume/trading activity does not support recovery")
        codes.append("volume")
    required_ratio = (
        policy.momentum_buy_min_ratio if is_momentum_buy
        else policy.early_buy_min_ratio if is_early_buy
        else policy.buy_sell_ratio
    )
    ratio_confirmed = _number(candidate.get("buy_sell_ratio")) >= required_ratio
    if is_momentum_buy:
        # A near-even transaction count can still mask a genuine pump if
        # buyers are moving meaningfully more size than sellers; liquidity
        # growth is direct evidence of real new capital that a count ratio
        # alone can't see, so it's accepted as an alternative confirmation.
        liquidity_growth_pct = (liquidity / baseline - 1) * 100 if baseline > 0 else None
        liquidity_confirmed = (
            liquidity_growth_pct is not None
            and liquidity_growth_pct >= policy.momentum_buy_min_liquidity_growth_pct
        )
        if not (ratio_confirmed or liquidity_confirmed):
            failures.append("buyer-to-seller ratio below minimum and liquidity is not growing enough")
            codes.append("buy_sell_ratio")
    elif not ratio_confirmed:
        failures.append("buyer-to-seller ratio below early-buy minimum" if is_early_buy
                        else "buyer-to-seller ratio below recovery minimum")
        codes.append("buy_sell_ratio")
    if is_momentum_buy:
        trade_count = _number(candidate.get("buys_m5")) + _number(candidate.get("sells_m5"))
        if trade_count < policy.momentum_buy_min_trades:
            failures.append(f"only {trade_count:.0f} five-minute trades, below momentum minimum {policy.momentum_buy_min_trades}")
            codes.append("trade_count")
    if is_early_buy:
        trade_count = _number(candidate.get("buys_m5")) + _number(candidate.get("sells_m5"))
        if trade_count < policy.early_buy_min_trades:
            failures.append(f"only {trade_count:.0f} five-minute trades, below early-buy minimum {policy.early_buy_min_trades}")
            codes.append("trade_count")
    if policy.require_medium_risk and risk not in {"MEDIUM", "MODERATE"}:
        failures.append("risk is outside permitted recovery band")
        codes.append("risk")
    if candidate.get("decision") == "AVOID":
        failures.append("recommendation decision is AVOID")
        codes.append("avoid_decision")
    if _number(candidate.get("signal_score")) < policy.min_score:
        failures.append("signal score below minimum")
        codes.append("signal_score")
    if count < required:
        failures.append(f"entry confirmations {count}/{required}")
        codes.append("confirmations")
    if candidate.get("decision") not in {"BUY ZONE", "BUY NOW", "MOMENTUM BUY", "EARLY BUY"}:
        failures.append("recommendation has no final buy decision")
        codes.append("no_decision")
    if not failures:
        state = "BUY_READY"
    elif pullback < policy.pullback_pct:
        state = "WATCH"
    elif momentum == "FALLING":
        state = "PULLBACK_STARTED"
    elif momentum in {"RISING", "STRONG"}:
        state = "RECOVERY_CONFIRMING"
    else:
        state = "STABILIZING"
    return {
        "state": state, "decision": "BUY" if not failures else "WATCH",
        "pullback_from_peak_pct": round(pullback, 4), "momentum": momentum,
        "risk": risk, "liquidity": "ACCEPTABLE" if liquidity >= policy.min_liquidity_usd else "LOW",
        "volume": volume, "entry_confirmations": f"{count}/{required}",
        "reasons": failures or ["pullback confirmed; momentum and activity support recovery; confirmations complete"],
        "failure_codes": codes,
    }


def assess_exit(
    position: dict[str, Any], quote: dict[str, Any], policy: ShadowRecoveryPolicy,
    *, now: float | None = None,
) -> dict[str, Any]:
    price = _number(quote.get("price"))
    peak = max(_number(position.get("highest_price_since_entry")), _number(position.get("entry_price")), price)
    entry = _number(position.get("entry_price"))
    if price <= 0 or entry <= 0:
        return {"state": "HOLD", "reasons": ["no usable price; position not marked"]}
    gain = (price / entry - 1) * 100
    peak_gain = (peak / entry - 1) * 100
    drawdown = (peak - price) / peak * 100
    liquidity = _number(quote.get("liquidity_usd"))
    baseline = _number(position.get("entry_liquidity_usd"))
    momentum_known = quote.get("price_change_m5_pct") is not None
    momentum = _number(quote.get("price_change_m5_pct"))
    sells = _number(quote.get("sells_m5"))
    buys = _number(quote.get("buys_m5"))
    volume_label = str(quote.get("volume_label") or "UNKNOWN").upper()
    opened_at = _epoch(position.get("opened_at"))
    # opened_at missing/invalid -> age 0, so the stagnation check below never
    # fires on data we don't actually have (never infer missing data as
    # grounds for an exit, same principle as the rest of this module).
    age_seconds = ((now if now is not None else time.time()) - opened_at) if opened_at > 0 else 0.0
    liquidity_failure = liquidity > 0 and (liquidity < policy.min_liquidity_usd or baseline > 0 and liquidity < baseline * (1 - policy.liquidity_drop_pct / 100))
    trend_break = momentum <= policy.momentum_exit_pct and sells >= 5 and sells / max(1, buys) >= policy.sell_pressure_ratio
    trailing_break = peak_gain >= policy.trailing_activation_pct and drawdown >= policy.trailing_stop_pct
    # Bought expecting a bounce; gave it the configured grace window, and
    # there's still no rise (gain below the bar) and no sign of one forming
    # (momentum non-positive) - exit proactively rather than wait for a
    # confirmed reversal that trend_break/trailing_break would eventually
    # catch anyway, likely at a worse price. volume_label isn't included
    # here: live_trial.py's cycle() doesn't currently populate it on the
    # quote passed in (it's always "UNKNOWN" there), so a volume-rising
    # requirement would be vacuous rather than a real gate.
    stagnant = (
        age_seconds >= policy.stagnation_window_seconds
        and gain < policy.stagnation_min_gain_pct
        and momentum_known
        and momentum <= 0
    )
    reasons = [f"return {gain:+.2f}%", f"post-entry peak drawdown {drawdown:.2f}%", f"momentum {momentum:+.2f}%"]
    if liquidity_failure and sells > buys:
        state = "EMERGENCY_EXIT"
        reasons.append("liquidity collapse with net selling")
    elif trend_break and (trailing_break or drawdown >= policy.trailing_stop_pct or quote.get("decision") == "EXIT WARNING"):
        state = "EXIT"
        reasons.append("confirmed reversal: momentum, selling and peak/structure evidence")
    elif trailing_break and (sells > buys or volume_label == "FALLING"):
        # A >=20% rise that has already pulled back >=12% from its peak,
        # corroborated by real sell pressure (sells_m5/buys_m5 are populated
        # by both live_trial.py and agent_cli.py's callers even when
        # volume_label itself is "UNKNOWN") or a falling volume label.
        state = "EXIT"
        reasons.append("trailing stop: price pulled back from a considerable peak, confirmed by selling pressure")
    elif stagnant:
        state = "EXIT"
        reasons.append(
            f"no sign of a rise {age_seconds / 60:.0f} min after entry "
            f"(gain {gain:+.2f}%, momentum {momentum:+.2f}%, volume {volume_label})"
        )
    elif trend_break or trailing_break:
        # One reversal condition without corroboration from the other -
        # await confirmation rather than exit on a single noisy signal.
        state = "REVERSAL_WARNING"
        reasons.append("one reversal condition; awaiting corroboration")
    elif gain > 0:
        state = "PROFIT_RUNNING"
    else:
        state = "HOLD"
    if state == "PROFIT_RUNNING":
        if price / entry >= policy.principal_multiple and not position.get("principal_recovered"):
            state = "TAKE_PARTIAL"
            reasons.append("configured principal recovery multiple reached")
        elif price / entry >= policy.half_profit_multiple and not position.get("second_stage_taken"):
            state = "TAKE_PARTIAL"
            reasons.append("configured second profit stage reached")
    return {"state": state, "reasons": reasons, "momentum": quote.get("momentum_label", "UNKNOWN"),
            "liquidity_usd": liquidity, "post_entry_peak": peak, "return_pct": round(gain, 4),
            "drawdown_from_post_entry_peak_pct": round(drawdown, 4)}
