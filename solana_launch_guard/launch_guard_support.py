from __future__ import annotations

import time

from .config import Settings
from .recommendations import RecommendationCandidate


def auto_buy_discovery_rejection(
    candidate: RecommendationCandidate,
    settings: Settings,
    *,
    now: float | None = None,
) -> str | None:
    """Return the first reason an automatic-discovery candidate is blocked."""
    if candidate.chain != "solana":
        return "only Solana candidates can be purchased"
    if candidate.decision not in {"BUY NOW", "BUY ZONE"}:
        return "candidate does not have a final buy decision"
    if candidate.mint in settings.auto_buy_excluded_mints:
        return "mint is excluded from automatic discovery"
    if candidate.signal_score < settings.auto_buy_discovery_min_score:
        return "signal score is below the automatic-discovery minimum"
    if (
        candidate.liquidity_usd is None
        or candidate.liquidity_usd
        < settings.auto_buy_discovery_min_liquidity_usd
    ):
        return "liquidity is below the automatic-discovery minimum"
    if (
        candidate.entry_confirmation_count
        < candidate.entry_confirmation_required
    ):
        return "entry confirmation is incomplete"
    age = (time.time() if now is None else now) - candidate.updated_at
    if age > settings.auto_buy_signal_max_age_seconds:
        return "signal is stale"
    return None


def _is_stock_token_symbol(
    symbol: str, stock_symbols: frozenset[str]
) -> bool:
    """Conservatively recognize direct, xStocks-suffixed, and wrapped
    Robinhood stock symbols."""
    normalized = symbol.strip().casefold()
    if normalized in stock_symbols:
        return True
    if (
        len(normalized) > 1
        and normalized.endswith("x")
        and normalized[:-1] in stock_symbols
    ):
        # Backed Finance's xStocks convention on Solana: a bare "x" suffix,
        # no "w" prefix (e.g. NVDAx for NVIDIA) - confirmed against the
        # actual on-chain token, distinct from the wrapped pattern below.
        return True
    return (
        len(normalized) > 2
        and normalized.startswith("w")
        and normalized.endswith("x")
        and normalized[1:-1] in stock_symbols
    )
