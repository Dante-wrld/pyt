import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "coin_tracker.py"
_SPEC = importlib.util.spec_from_file_location("coin_tracker", _PATH)
coin_tracker = importlib.util.module_from_spec(_SPEC)
sys.modules["coin_tracker"] = coin_tracker
_SPEC.loader.exec_module(coin_tracker)

NOW = 1_000_000_000.0
DAY_MS = 86_400_000


def row(mint, *, age_days=5, liquidity=80_000, score=50, chain="solana"):
    return {
        "mint": mint, "chain": chain, "signal_score": score,
        "liquidity_usd": liquidity,
        "pair_created_at_ms": NOW * 1000 - age_days * DAY_MS,
    }


def cfg(tmp_path, **overrides):
    base = dict(coin_tracker.CONFIG)
    base.update(
        pinned=["PIN"], board_min_age_days=3, board_min_liquidity_usd=50_000,
        board_max_tokens=4, board_refresh_seconds=300,
        board_max_snapshot_age_seconds=900,
        board_snapshot_path=str(tmp_path / "board.json"),
        state_file=str(tmp_path / "state.json"),
    )
    base.update(overrides)
    return base


def write_board(tmp_path, rows, generated_at=NOW):
    (tmp_path / "board.json").write_text(
        json.dumps({"generated_at": generated_at, "candidates": rows})
    )


def test_select_applies_live_filters_and_ranks_by_score(tmp_path):
    rows = [
        row("YOUNG", age_days=1, score=99),
        row("THIN", liquidity=10_000, score=98),
        row("EVM", chain="base", score=97),
        row("NOAGE", score=96) | {"pair_created_at_ms": None},
        row("LOW", score=10),
        row("HIGH", score=90),
        row("MID", score=50),
    ]
    chosen = coin_tracker.select_watchlist(rows, ["PIN"], [], cfg(tmp_path), NOW)
    assert chosen == ["PIN", "HIGH", "MID", "LOW"]


def test_cap_counts_pinned_and_held_first(tmp_path):
    rows = [row(f"B{i}", score=i) for i in range(10)]
    chosen = coin_tracker.select_watchlist(
        rows, ["PIN"], ["HELD1", "HELD2"], cfg(tmp_path), NOW
    )
    assert chosen == ["PIN", "HELD1", "HELD2", "B9"]


def test_stale_or_missing_board_is_ignored(tmp_path):
    assert coin_tracker.read_board(tmp_path / "nope.json", 900, NOW) is None
    write_board(tmp_path, [row("A")], generated_at=NOW - 3600)
    assert coin_tracker.read_board(tmp_path / "board.json", 900, NOW) is None
    write_board(tmp_path, [row("A")])
    assert coin_tracker.read_board(tmp_path / "board.json", 900, NOW)[0]["mint"] == "A"


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setattr(coin_tracker, "DexScreenerOracle", lambda: None)
    return coin_tracker.Bot(cfg(tmp_path), coin_tracker.PaperExecutor())


def test_refresh_adds_and_drops_but_keeps_open_positions(tmp_path, bot):
    write_board(tmp_path, [row("A", score=9), row("B", score=8)])
    bot.refresh_watchlist(NOW)
    assert bot.watchlist == ["PIN", "A", "B"]
    assert set(bot.hist) == {"PIN", "A", "B"}

    bot.positions["A"] = coin_tracker.Position(
        qty=1.0, cost_usd=5.0, peak=1.0, opened=NOW, last_high=NOW
    )
    write_board(tmp_path, [row("C", score=9)], generated_at=NOW + 400)
    bot.refresh_watchlist(NOW + 400)
    assert bot.watchlist == ["PIN", "A", "C"]  # A held, B dropped
    assert "B" not in bot.hist


def test_refresh_respects_interval_and_stale_board(tmp_path, bot):
    write_board(tmp_path, [row("A")])
    bot.refresh_watchlist(NOW)
    write_board(tmp_path, [row("Z")])
    bot.refresh_watchlist(NOW + 10)  # too soon
    assert "Z" not in bot.watchlist
    bot.refresh_watchlist(NOW + 5000)  # board file is now stale
    assert bot.watchlist == ["PIN", "A"]


def test_watchlist_survives_restart(tmp_path, bot, monkeypatch):
    write_board(tmp_path, [row("A")])
    bot.refresh_watchlist(NOW)
    bot.save()
    again = coin_tracker.Bot(cfg(tmp_path), coin_tracker.PaperExecutor())
    assert again.watchlist == ["PIN", "A"]
    assert set(again.hist) == {"PIN", "A"}


def test_static_mode_never_reads_the_board(tmp_path, monkeypatch):
    monkeypatch.setattr(coin_tracker, "DexScreenerOracle", lambda: None)
    static = coin_tracker.Bot(
        cfg(tmp_path, dynamic_watchlist=False), coin_tracker.PaperExecutor()
    )
    write_board(tmp_path, [row("A")])
    static.refresh_watchlist(NOW)
    assert static.watchlist == ["PIN"]


def test_dip_buys_are_off_by_default():
    assert coin_tracker.CONFIG["dip_buys_enabled"] is False
