"""One place that says which feeds run and which signals may spend money.

Before this, a feed was on whenever its credentials happened to be present,
and every buy decision type could reach a live order. Both are now explicit
settings, so the live setup is one readable block in ``.env``.

Defaults preserve the previous behaviour exactly (every feed on, every buy
decision allowed, no age floor); nothing changes until you opt in.

The entry gate only blocks *orders*. Blocked candidates are still scored,
shown on the board, alerted, written to the ledger, and recorded by the
outcome tracker, which is what "shadow only" means here: the evidence keeps
accumulating without the money.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .config import _bool, _csv_upper, _float

ALL_BUY_DECISIONS = ("BUY NOW", "BUY ZONE", "MOMENTUM BUY", "EARLY BUY")


@dataclass(frozen=True, slots=True)
class StrategyProfile:
    feed_pumpportal_launches: bool = True
    feed_launchlab: bool = True
    feed_solana_momentum: bool = True
    feed_copyfomo_wallets: bool = True
    feed_multichain: bool = True
    feed_leader_holdings: bool = False
    entry_allowed_decisions: tuple[str, ...] = ALL_BUY_DECISIONS
    entry_min_token_age_days: float = 0.0
    # A token that ONLY these sources found is never bought live; it is still
    # scored, signalled and tracked. A token another feed also found is not
    # affected, since that feed alone would have put it on the board.
    entry_shadow_only_sources: tuple[str, ...] = ("leader-held",)

    @classmethod
    def from_env(cls) -> StrategyProfile:
        allowed = _csv_upper("ENTRY_ALLOWED_DECISIONS", ",".join(ALL_BUY_DECISIONS))
        unknown = set(allowed) - set(ALL_BUY_DECISIONS)
        if unknown:
            raise ValueError(
                "ENTRY_ALLOWED_DECISIONS contains unknown decisions: "
                + ", ".join(sorted(unknown))
                + f" (choose from {', '.join(ALL_BUY_DECISIONS)})"
            )
        return cls(
            feed_pumpportal_launches=_bool("FEED_PUMPPORTAL_LAUNCHES", True),
            feed_launchlab=_bool("FEED_LAUNCHLAB", True),
            feed_solana_momentum=_bool("FEED_SOLANA_MOMENTUM", True),
            feed_copyfomo_wallets=_bool("FEED_COPYFOMO_WALLETS", True),
            feed_multichain=_bool("FEED_MULTICHAIN", True),
            feed_leader_holdings=_bool("FEED_LEADER_HOLDINGS", False),
            entry_allowed_decisions=allowed,
            entry_min_token_age_days=_float("ENTRY_MIN_TOKEN_AGE_DAYS", 0.0),
            entry_shadow_only_sources=tuple(
                s.lower()
                for s in _csv_upper("ENTRY_SHADOW_ONLY_SOURCES", "leader-held")
            ),
        )

    def entry_block_reason(
        self,
        decision: str | None,
        pair_created_at_ms: float | None,
        *,
        sources: list[str] | tuple[str, ...] | None = None,
        now: float | None = None,
    ) -> str | None:
        """Why a live order must not be placed for this signal, or None."""
        if decision not in self.entry_allowed_decisions:
            return f"{decision} is shadow-only (not in ENTRY_ALLOWED_DECISIONS)"
        if sources and set(sources) <= set(self.entry_shadow_only_sources):
            return (
                "found only by shadow-only source "
                + ", ".join(sorted(set(sources)))
                + " (ENTRY_SHADOW_ONLY_SOURCES)"
            )
        if self.entry_min_token_age_days > 0:
            if pair_created_at_ms is None or pair_created_at_ms <= 0:
                return "token age is unknown and ENTRY_MIN_TOKEN_AGE_DAYS is set"
            at = time.time() if now is None else now
            age_days = (at * 1000 - pair_created_at_ms) / 86_400_000
            if age_days < self.entry_min_token_age_days:
                return (
                    f"token is {age_days:.1f}d old, below "
                    f"ENTRY_MIN_TOKEN_AGE_DAYS={self.entry_min_token_age_days:g}"
                )
        return None

    def describe(self) -> str:
        feeds = {
            "pumpportal": self.feed_pumpportal_launches,
            "launchlab": self.feed_launchlab,
            "solana-momentum": self.feed_solana_momentum,
            "copyfomo-wallets": self.feed_copyfomo_wallets,
            "multichain": self.feed_multichain,
            "leader-holdings": self.feed_leader_holdings,
        }
        on = [name for name, enabled in feeds.items() if enabled] or ["none"]
        off = [name for name, enabled in feeds.items() if not enabled] or ["none"]
        age = (
            f"{self.entry_min_token_age_days:g}d+"
            if self.entry_min_token_age_days > 0
            else "any age"
        )
        return (
            f"feeds on: {', '.join(on)} | off: {', '.join(off)} | live entries: "
            f"{', '.join(self.entry_allowed_decisions) or 'none'} on tokens {age}"
        )
