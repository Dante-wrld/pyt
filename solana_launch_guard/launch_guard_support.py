from __future__ import annotations

import json
import time
from pathlib import Path

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
    if candidate.decision not in {"BUY NOW", "BUY ZONE", "MOMENTUM BUY", "EARLY BUY"}:
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


def hunter_v1_is_at_capacity(*, path: str, max_age_seconds: float = 90.0) -> bool:
    """Read-only, one-way consumer of live_trial.py's per-cycle capacity
    snapshot (_write_hunter_capacity_snapshot) - lets a discovery feed
    (LaunchLab, Solana momentum) throttle its own metered-API polling while
    hunter-v1 has no room for a new fresh position anyway. Fails open on
    anything: a missing file (no live trial running, or this feature
    predates it), a malformed one, or a stale one (the live trial stopped
    writing it - default cycle interval is ~30s, so 90s is a few missed
    cycles, not a hair trigger) all read as "not at capacity", so a feed
    reading this can never be silently starved by the snapshot going away.
    """
    try:
        snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(snapshot, dict):
        return False
    try:
        age = time.time() - float(snapshot.get("generated_at") or 0)
    except (TypeError, ValueError):
        return False
    if not (0 <= age <= max_age_seconds):
        return False
    return bool(snapshot.get("hunter_v1_at_capacity"))


def _is_stock_token_symbol(
    symbol: str, stock_symbols: frozenset[str]
) -> bool:
    """Conservatively recognize direct, xStocks-suffixed, and wrapped
    Robinhood stock symbols."""
    normalized = symbol.strip().casefold()
    if normalized in stock_symbols:
        return True
    if (
        len(normalized) > 2
        and normalized.endswith("x")
        and normalized[:-1] in stock_symbols
    ):
        # Backed Finance's xStocks convention on Solana: a bare "x" suffix,
        # no "w" prefix (e.g. NVDAx for NVIDIA) - confirmed against the
        # actual on-chain token, distinct from the wrapped pattern below.
        # Requires at least a 2-character ticker before the "x" so this
        # doesn't collide with single-letter tickers (F, T, C, X, ...) that
        # happen to be a prefix of an unrelated two-letter meme symbol.
        return True
    return (
        len(normalized) > 2
        and normalized.startswith("w")
        and normalized.endswith("x")
        and normalized[1:-1] in stock_symbols
    )
