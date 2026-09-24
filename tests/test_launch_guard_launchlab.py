"""price_move_pct/is_hot_baseline: the pure trim-decision helpers, and
_trim_check's promote/drop/retry behavior, behind LaunchLab's
discover-then-confirm funnel (see launch_guard_launchlab.py's module
docstring)."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from solana_launch_guard.intelligence import IntelligenceResult
from solana_launch_guard.launch_guard_launchlab import (
    LaunchLabFeedMixin, _Pending, is_hot_baseline, price_move_pct,
)
from solana_launch_guard.market import MarketQuote

MINT = "M" * 44


def _quote(*, mint=MINT, symbol="TEST", price_usd=1.0, buys_m5=0, sells_m5=0):
    return MarketQuote(
        mint=mint, symbol=symbol, price_sol=price_usd, liquidity_usd=50_000.0,
        market_cap_usd=200_000.0, pair_address="pool", pair_created_at_ms=0,
        buys_m5=buys_m5, sells_m5=sells_m5, volume_m5_usd=1000.0,
        price_change_m5_pct=0.0, price_usd=price_usd,
    )


def _result(*, tier="CORE", score=80):
    return IntelligenceResult(
        tier=tier, total_score=score, safety_score=score,
        momentum_score=score, reasons=("ok",),
    )


class _FakeClient:
    def __init__(self):
        self.requested: list[str] = []

    async def launchlab_activity_for_mints(self, mints):
        self.requested = list(mints)
        return [], []


def _feed(*, score_by_mint, min_move_pct=5.0, retry_window=300.0):
    feed = LaunchLabFeedMixin()
    feed.settings = SimpleNamespace(
        launchlab_trim_min_price_move_pct=min_move_pct,
        launchlab_trim_retry_window_seconds=retry_window,
    )
    feed.intelligence = SimpleNamespace(score=lambda quote: score_by_mint[quote.mint])
    saved: list[dict] = []
    added: list[tuple] = []
    feed.store = SimpleNamespace(save_intelligence_score=lambda **kwargs: saved.append(kwargs))
    feed.recommendations = SimpleNamespace(add=lambda quote, result: added.append((quote, result)))
    return feed, saved, added


def test_price_move_pct_computes_absolute_percent_change():
    assert price_move_pct(1.0, 1.05) == pytest.approx(5.0)
    assert price_move_pct(1.0, 0.95) == pytest.approx(5.0)


def test_price_move_pct_is_zero_when_baseline_missing():
    assert price_move_pct(None, 1.0) == 0.0


def test_price_move_pct_is_zero_when_baseline_non_positive():
    assert price_move_pct(0.0, 1.0) == 0.0
    assert price_move_pct(-1.0, 1.0) == 0.0


def test_price_move_pct_is_zero_when_fresh_missing():
    # Observed shape: a trim-checked mint whose activity vanished entirely
    # (build_launchlab_quotes drops it) - fail closed to "hasn't moved",
    # not an exception or a fabricated large move.
    assert price_move_pct(1.0, None) == 0.0


def test_price_move_pct_is_zero_when_fresh_is_zero():
    assert price_move_pct(1.0, 0.0) == 0.0


def test_is_hot_baseline_true_once_combined_trades_clear_the_bar():
    quote = _quote(buys_m5=10, sells_m5=5)
    assert is_hot_baseline(quote, min_trades=15) is True


def test_is_hot_baseline_false_below_the_bar():
    quote = _quote(buys_m5=10, sells_m5=4)
    assert is_hot_baseline(quote, min_trades=15) is False


def _run_trim_check(feed, client, pending, due_mints):
    asyncio.run(feed._trim_check(client, pending, due_mints))


def test_trim_check_promotes_when_score_and_move_both_clear(monkeypatch):
    fresh = _quote(price_usd=1.06)
    monkeypatch.setattr(
        "solana_launch_guard.launch_guard_launchlab.build_launchlab_quotes",
        lambda *, trades, pools, creations: {MINT: fresh},
    )
    feed, saved, added = _feed(score_by_mint={MINT: _result(tier="CORE")})
    pending = {MINT: _Pending(baseline=_quote(price_usd=1.0), due_at=0.0)}
    _run_trim_check(feed, _FakeClient(), pending, [MINT])

    assert pending == {}
    assert len(added) == 1
    assert added[0][0] is fresh
    assert len(saved) == 1


def test_trim_check_drops_immediately_when_score_fails_no_retry(monkeypatch):
    # Unmoved AND failing score would also hit the not-accepted branch -
    # move it enough that only the score bar is what's actually tested.
    fresh = _quote(price_usd=1.06)
    monkeypatch.setattr(
        "solana_launch_guard.launch_guard_launchlab.build_launchlab_quotes",
        lambda *, trades, pools, creations: {MINT: fresh},
    )
    feed, saved, added = _feed(score_by_mint={MINT: _result(tier="REJECT")})
    pending = {MINT: _Pending(baseline=_quote(price_usd=1.0), due_at=0.0)}
    _run_trim_check(feed, _FakeClient(), pending, [MINT])

    # No retry for a token that failed the safety/score bar outright.
    assert pending == {}
    assert added == []
    assert saved == []


def test_trim_check_retries_once_when_accepted_but_not_moved_enough(monkeypatch):
    fresh = _quote(price_usd=1.0)  # identical to baseline - 0% move
    monkeypatch.setattr(
        "solana_launch_guard.launch_guard_launchlab.build_launchlab_quotes",
        lambda *, trades, pools, creations: {MINT: fresh},
    )
    feed, saved, added = _feed(
        score_by_mint={MINT: _result(tier="CORE")}, retry_window=300.0,
    )
    pending = {MINT: _Pending(baseline=_quote(price_usd=1.0), due_at=0.0)}
    before = time.time()
    _run_trim_check(feed, _FakeClient(), pending, [MINT])

    assert added == []
    assert saved == []
    assert MINT in pending
    entry = pending[MINT]
    assert entry.retries_used == 1
    assert entry.due_at >= before + 300.0


def test_trim_check_drops_after_the_retry_is_exhausted(monkeypatch):
    fresh = _quote(price_usd=1.0)
    monkeypatch.setattr(
        "solana_launch_guard.launch_guard_launchlab.build_launchlab_quotes",
        lambda *, trades, pools, creations: {MINT: fresh},
    )
    feed, saved, added = _feed(score_by_mint={MINT: _result(tier="CORE")})
    # Already used its one retry - this trim-check is its second look.
    pending = {MINT: _Pending(baseline=_quote(price_usd=1.0), due_at=0.0, retries_used=1)}
    _run_trim_check(feed, _FakeClient(), pending, [MINT])

    assert pending == {}
    assert added == []
    assert saved == []


def test_trim_check_drops_when_fresh_quote_is_missing(monkeypatch):
    monkeypatch.setattr(
        "solana_launch_guard.launch_guard_launchlab.build_launchlab_quotes",
        lambda *, trades, pools, creations: {},
    )
    feed, saved, added = _feed(score_by_mint={})
    pending = {MINT: _Pending(baseline=_quote(price_usd=1.0), due_at=0.0)}
    _run_trim_check(feed, _FakeClient(), pending, [MINT])

    assert pending == {}
    assert added == []
    assert saved == []
