from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from .config import Settings
from .market import MarketQuote


def auto_rebuy_recovery_assessment(
    watch: Mapping[str, Any],
    quote: MarketQuote | None,
    settings: Settings,
    *,
    now: float | None = None,
    advisory: bool = False,
) -> tuple[bool, str, dict[str, float]]:
    """Evaluate an opt-in post-sale recovery without predicting a rebound."""
    observed_at = time.time() if now is None else now
    age = observed_at - float(watch["sold_at_epoch"])
    if age < settings.auto_rebuy_cooldown_seconds:
        remaining = settings.auto_rebuy_cooldown_seconds - age
        return False, f"cooldown has {remaining:.0f}s remaining", {}
    if not advisory and age > settings.auto_rebuy_max_watch_seconds:
        return False, "recovery watch expired", {"age_seconds": age}
    if quote is None or quote.price_usd is None or quote.price_usd <= 0:
        return False, "USD market quote unavailable", {}

    price = quote.price_usd
    exit_price = float(watch["exit_price_usd"])
    low = min(float(watch["lowest_price_usd"]), price)
    drop_pct = max(0.0, (1 - low / exit_price) * 100)
    rebound_pct = max(0.0, (price / low - 1) * 100)
    discount_pct = (1 - price / exit_price) * 100
    momentum_pct = quote.price_change_m5_pct or 0.0
    ratio = quote.buy_sell_ratio
    liquidity = quote.liquidity_usd or 0.0
    exit_liquidity = float(watch["exit_liquidity_usd"] or 0.0)
    retention_pct = (
        liquidity / exit_liquidity * 100 if exit_liquidity > 0 else 100.0
    )
    previous_price = watch.get("last_price_usd")
    rising = previous_price is not None and price > float(previous_price)
    metrics = {
        "age_seconds": age,
        "price_usd": price,
        "lowest_price_usd": low,
        "drop_pct": drop_pct,
        "rebound_pct": rebound_pct,
        "entry_discount_pct": discount_pct,
        "momentum_pct": momentum_pct,
        "buy_sell_ratio": ratio,
        "liquidity_usd": liquidity,
        "liquidity_retention_pct": retention_pct,
    }

    rejections: list[str] = []
    if drop_pct < settings.auto_rebuy_min_drop_pct:
        rejections.append(
            f"drop {drop_pct:.1f}% is below "
            f"{settings.auto_rebuy_min_drop_pct:.1f}%"
        )
    if rebound_pct < settings.auto_rebuy_min_rebound_pct:
        rejections.append(
            f"rebound {rebound_pct:.1f}% is below "
            f"{settings.auto_rebuy_min_rebound_pct:.1f}%"
        )
    if discount_pct < settings.auto_rebuy_min_entry_discount_pct:
        rejections.append(
            f"entry discount {discount_pct:.1f}% is below "
            f"{settings.auto_rebuy_min_entry_discount_pct:.1f}%"
        )
    if momentum_pct < settings.auto_rebuy_min_momentum_pct:
        rejections.append(
            f"5m momentum {momentum_pct:.1f}% is below "
            f"{settings.auto_rebuy_min_momentum_pct:.1f}%"
        )
    if ratio < settings.auto_rebuy_min_buy_sell_ratio:
        rejections.append(
            f"buyer/seller ratio {ratio:.2f}x is below "
            f"{settings.auto_rebuy_min_buy_sell_ratio:.2f}x"
        )
    if quote.buys_m5 < settings.auto_rebuy_min_buys_m5:
        rejections.append(
            f"5m buys {quote.buys_m5} are below "
            f"{settings.auto_rebuy_min_buys_m5}"
        )
    if liquidity < settings.auto_rebuy_min_liquidity_usd:
        rejections.append(
            f"liquidity ${liquidity:,.0f} is below "
            f"${settings.auto_rebuy_min_liquidity_usd:,.0f}"
        )
    if retention_pct < settings.auto_rebuy_min_liquidity_retention_pct:
        rejections.append(
            f"liquidity retention {retention_pct:.1f}% is below "
            f"{settings.auto_rebuy_min_liquidity_retention_pct:.1f}%"
        )
    if not rising:
        rejections.append("price is not rising versus the prior poll")
    if rejections:
        return False, "; ".join(rejections), metrics
    return (
        True,
        (
            f"recovery confirmed: {drop_pct:.1f}% drop, "
            f"{rebound_pct:.1f}% rebound, {momentum_pct:+.1f}% momentum"
        ),
        metrics,
    )


