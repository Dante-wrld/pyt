"""Turns a leader's own BUY (seen read-only, via CopyFomoMonitorMixin's
existing wallet watcher) into a signal copy-v1 can act on.

This is the direct counterpart to launch_guard_leader_holdings.py, which
deliberately does NOT copy a leader's trade ("buying after them... the copy
is always late") and instead surfaces what a leader currently holds. That
tradeoff is right for hunter-v1, which has its own independent entry rules
and just wants more candidates to look at. copy-v1 is the opposite case: its
whole mandate is "should I follow THIS leader's THIS trade" (AgentRole.
COPY_TRADER, "Discover and score traders, then selectively propose
attributed copies; never blindly mirror" - see agents.py), and that
question is only meaningful while the trade is still fresh.

A signal expires after COPY_SIGNAL_TTL_SECONDS un-acted-on. A small
background task re-quotes pending signals periodically so
launch_guard_copy_signals.json stays inside agent_cli.py's 15-second
freshness window, and adds the mint to the main board (source="leader-copy",
in ENTRY_SHADOW_ONLY_SOURCES by default - same live-money protection as
leader-held) so it also gets a continuously fresh quote from the main poll
loop independent of this task's own interval, which is what lets copy-v1's
buy actually fill (agent_cli.py's fresh_quotes needs the mint there).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import _float
from .launch_guard_state import LaunchGuardState
from .launch_guard_support import _is_stock_token_symbol
from .market import MarketQuote

LOGGER = logging.getLogger("solana_launch_guard")

SOURCE = "leader-copy"


@dataclass(frozen=True, slots=True)
class CopySignalConfig:
    poll_seconds: float = 10.0
    ttl_seconds: float = 1800.0
    min_usd: float = 50.0
    snapshot_path: str = "launch_guard_copy_signals.json"

    @classmethod
    def from_env(cls) -> CopySignalConfig:
        return cls(
            poll_seconds=max(5.0, _float("COPY_SIGNAL_POLL_SECONDS", 10.0)),
            ttl_seconds=max(60.0, _float("COPY_SIGNAL_TTL_SECONDS", 1800.0)),
            min_usd=_float("COPY_SIGNAL_MIN_USD", 50.0),
            snapshot_path=os.getenv(
                "AGENT_COPY_SIGNAL_PATH", "launch_guard_copy_signals.json"
            ),
        )


@dataclass(frozen=True, slots=True)
class PendingCopySignal:
    leader: str
    leader_wallet: str
    mint: str
    entry_price_usd: float
    detected_at: float


def prune_expired(
    pending: Mapping[str, PendingCopySignal], *, now: float, ttl_seconds: float
) -> dict[str, PendingCopySignal]:
    return {
        mint: sig for mint, sig in pending.items()
        if now - sig.detected_at <= ttl_seconds
    }


def build_copy_signals(
    pending: Mapping[str, PendingCopySignal],
    quotes: Mapping[str, MarketQuote | None],
    *,
    now: float,
    stock_symbols: frozenset[str] | None = None,
) -> list[dict[str, object]]:
    """One signal dict per still-priced, non-stock pending mint - the shape
    agent_cli.py's copy-v1 context and fresh_quotes merge both expect."""
    signals = []
    for mint, sig in pending.items():
        quote = quotes.get(mint)
        if quote is None or not quote.price_usd or quote.price_usd <= 0:
            continue
        if stock_symbols is not None and _is_stock_token_symbol(
            quote.symbol, stock_symbols
        ):
            continue
        move_pct = (
            (quote.price_usd / sig.entry_price_usd - 1) * 100
            if sig.entry_price_usd > 0 else 0.0
        )
        signals.append({
            "leader": sig.leader, "leader_wallet": sig.leader_wallet,
            "mint": mint, "chain": "solana", "symbol": quote.symbol,
            "price": quote.price_usd, "price_currency": "USD",
            "liquidity_usd": quote.liquidity_usd, "pair_address": quote.pair_address,
            "pair_created_at_ms": quote.pair_created_at_ms,
            "price_move_since_entry_pct": move_pct,
            "quoted_at": now, "detected_at": sig.detected_at,
        })
    return signals


def _write_snapshot(path: str, signals: list[dict[str, object]]) -> None:
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps({"generated_at": time.time(), "signals": signals}))
    tmp.replace(path)


class CopySignalFeedMixin(LaunchGuardState):
    """Maintains launch_guard_copy_signals.json from leader BUYs that
    CopyFomoMonitorMixin's wallet watcher observes."""

    def record_leader_buy(
        self, leader: str, leader_wallet: str, mint: str,
        price_usd: float | None, usd_value: float,
    ) -> None:
        config = CopySignalConfig.from_env()
        if usd_value < config.min_usd:
            return
        self.copy_signals[mint] = PendingCopySignal(
            leader=leader, leader_wallet=leader_wallet, mint=mint,
            entry_price_usd=price_usd or 0.0, detected_at=time.time(),
        )

    async def run_copy_signal_feed(self) -> None:
        config = CopySignalConfig.from_env()
        LOGGER.info(
            "Copy-signal feed active: poll %.0fs, ttl %.0fs, min $%.0f (source=%s)",
            config.poll_seconds, config.ttl_seconds, config.min_usd, SOURCE,
        )
        while True:
            try:
                await self.poll_copy_signals(config)
            except (OSError, RuntimeError, ValueError) as exc:
                LOGGER.warning("Copy-signal feed: poll failed: %s", exc)
            await asyncio.sleep(config.poll_seconds)

    async def poll_copy_signals(
        self, config: CopySignalConfig
    ) -> list[dict[str, object]]:
        now = time.time()
        # In-place update, not `self.copy_signals = ...`: a full reassignment
        # of a Protocol-declared dict attribute, from a mixin that also
        # overrides a Protocol method elsewhere in the class (record_leader_
        # buy here), breaks mypy's attribute-type inference for the whole
        # class (reproduced in isolation; fixed by never rebinding it).
        pruned = prune_expired(
            self.copy_signals, now=now, ttl_seconds=config.ttl_seconds
        )
        self.copy_signals.clear()
        self.copy_signals.update(pruned)
        if not self.copy_signals:
            _write_snapshot(config.snapshot_path, [])
            return []
        quotes = await self.oracle.quote_many(list(self.copy_signals), chain="solana")
        stock_symbols = await self.oracle.robinhood_stock_token_symbols()
        signals = build_copy_signals(
            self.copy_signals, quotes, now=now, stock_symbols=stock_symbols
        )
        for signal in signals:
            mint = signal["mint"]
            assert isinstance(mint, str)
            quote = quotes[mint]
            assert quote is not None
            result = self.intelligence.score(quote)
            if not result.accepted:
                continue
            candidate = self.recommendations.add(quote, result, source=SOURCE)
            if candidate is not None:
                LOGGER.info(
                    "LEADER-COPY %-10s bought by %s (%+.1f%% since) score=%d mint=%s",
                    quote.symbol, signal["leader"],
                    signal["price_move_since_entry_pct"], result.total_score,
                    signal["mint"],
                )
        _write_snapshot(config.snapshot_path, signals)
        return signals
