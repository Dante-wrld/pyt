import asyncio
import sqlite3
from datetime import UTC, datetime

import pytest
from solana_launch_guard.core import SQLiteStore
from solana_launch_guard.eval_cli import build_report
from solana_launch_guard.evaluation import (
    CostModel,
    ExitRules,
    Observation,
    TrackedDecision,
)
from solana_launch_guard.market_structure import Candle, classify_candle_pattern
from solana_launch_guard.outcome_tracker import (
    OutcomeStore,
    OutcomeTracker,
    TrackerConfig,
)
from solana_launch_guard.strategy_profile import StrategyProfile

from test_buy_signals import candidate, guard_with
from test_evaluation import FakeClient, _make_launch_db

NOW = 10_000.0


def bars(last, trend="flat"):
    """A prior candle whose close sets the trend, then `last` (o, h, l, c).
    The classifier compares this open with the close five bars back."""
    o, h, low, c = last
    prior_close = {"up": o - 0.05, "down": o + 0.05, "flat": o}[trend]
    start = NOW - 60 * 7
    out = [Candle(int(start), prior_close, prior_close, prior_close, prior_close, 10)]
    out += [Candle(int(start + 60 * i), o, o, o, o, 10) for i in range(1, 5)]
    out.append(Candle(int(NOW - 60 * 2 + 30), o, h, low, c, 10))
    return out


@pytest.mark.parametrize(("ohlc", "trend", "expected"), [
    ((1.0, 1.1, 1.0, 1.1), "flat", "bullish_marubozu"),
    ((1.1, 1.1, 1.0, 1.0), "flat", "bearish_marubozu"),
    ((1.08, 1.10, 1.00, 1.10), "down", "hammer"),
    ((1.08, 1.10, 1.00, 1.10), "up", "hanging_man"),
    ((1.00, 1.10, 1.00, 1.02), "down", "inverted_hammer"),
    ((1.00, 1.10, 1.00, 1.02), "up", "shooting_star"),
    ((1.10, 1.10, 1.00, 1.10), "flat", "dragonfly_doji"),
    ((1.00, 1.10, 1.00, 1.00), "flat", "gravestone_doji"),
    ((1.05, 1.10, 1.00, 1.05), "flat", "long_legged_doji"),
    ((1.04, 1.10, 1.00, 1.06), "flat", "spinning_top"),
])
def test_patterns_from_the_chart(ohlc, trend, expected):
    assert classify_candle_pattern(bars(ohlc, trend), now=NOW)["pattern"] == expected


def test_stale_or_broken_candles_are_unavailable_not_guessed():
    assert classify_candle_pattern([], now=NOW)["pattern"] == "unavailable"
    old = bars((1.0, 1.1, 1.0, 1.1))
    assert classify_candle_pattern(old, now=NOW + 3600)["pattern"] == "unavailable"
    broken = bars((1.0, 0.9, 1.0, 1.1))  # high below close
    assert classify_candle_pattern(broken, now=NOW)["pattern"] == "unavailable"


def test_existing_database_gets_the_new_columns(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE buy_signals (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "signaled_at TEXT NOT NULL, mint TEXT NOT NULL, symbol TEXT NOT NULL, "
        "chain TEXT NOT NULL, decision TEXT NOT NULL, price REAL, "
        "price_currency TEXT, liquidity_usd REAL, signal_score INTEGER, "
        "pair_created_at_ms INTEGER, reason TEXT, live_blocked_reason TEXT)"
    )
    db.commit()
    db.close()
    store = SQLiteStore(str(path))
    columns = {r[1] for r in store.connection.execute("PRAGMA table_info(buy_signals)")}
    assert {"candle_pattern", "candle_trend"} <= columns
    store.close()


def test_signal_is_tagged_in_the_background(tmp_path):
    store, guard = guard_with(tmp_path, StrategyProfile())

    class Scanner:
        async def _closed_minute_candles(self, *, pool, mint):
            import time
            now = time.time()
            return [Candle(int(now - 90), 1.0, 1.1, 1.0, 1.1, 10)]

    guard.signal_candle_scanner = Scanner()
    guard.recommendations.candidates["A"] = candidate("A", "MOMENTUM BUY")

    async def run():
        guard._record_buy_signals()
        await asyncio.gather(*guard.signal_tag_tasks)

    asyncio.run(run())
    row = store.connection.execute(
        "SELECT candle_pattern FROM buy_signals"
    ).fetchone()
    assert row[0] == "bullish_marubozu"
    store.close()


def test_a_failing_candle_fetch_never_breaks_the_monitor(tmp_path):
    store, guard = guard_with(tmp_path, StrategyProfile())

    class Broken:
        async def _closed_minute_candles(self, *, pool, mint):
            raise RuntimeError("gecko down")

    guard.signal_candle_scanner = Broken()
    guard.recommendations.candidates["A"] = candidate("A", "BUY ZONE")

    async def run():
        guard._record_buy_signals()
        await asyncio.gather(*guard.signal_tag_tasks)

    asyncio.run(run())  # logs a warning, does not raise
    count = store.connection.execute("SELECT COUNT(*) FROM buy_signals").fetchone()
    assert count[0] == 1
    store.close()


def test_tracker_backfills_tags_that_arrive_after_ingest(tmp_path):
    launch_db = tmp_path / "launch_guard.db"
    _make_launch_db(launch_db, [])
    SQLiteStore(str(launch_db)).close()
    db = sqlite3.connect(launch_db)
    db.execute(
        "INSERT INTO buy_signals(signaled_at, mint, symbol, chain, decision) "
        "VALUES (?, 'A', 'A', 'solana', 'MOMENTUM BUY')",
        (datetime.fromtimestamp(1000.0, UTC).isoformat(),),
    )
    db.commit()
    store = OutcomeStore(tmp_path / "o.db")
    tracker = OutcomeTracker(store, FakeClient({"A": 1.0}), TrackerConfig(),
                             launch_db=launch_db, ledger_db=None, clock=lambda: 1010.0)
    tracker.ingest()
    assert store.untagged_signal_ids() == [1]
    db.execute("UPDATE buy_signals SET candle_pattern = 'hammer' WHERE id = 1")
    db.commit()
    db.close()
    tracker.ingest()
    assert store.untagged_signal_ids() == []
    [decision] = store.load()
    assert "candle: hammer" in decision.reasons
    store.close()


def test_report_splits_each_signal_type_by_pattern():
    def signal(i, tag, exit_price):
        obs = (Observation(i * 1000 + 5, 1.0, 9000, True),
               Observation(i * 1000 + 65, exit_price, 9000, True))
        reasons = (f"candle: {tag}",) if tag else ()
        return TrackedDecision("signal", f"m{i}", i * 1000.0, "MOMENTUM BUY",
                               reasons, obs)
    decisions = [signal(i, "bullish_marubozu", 1.4) for i in range(3)]
    decisions += [signal(10 + i, "shooting_star", 0.7) for i in range(3)]
    decisions += [signal(20 + i, None, 1.0) for i in range(3)]
    report = build_report(decisions, ExitRules(), CostModel(), train_fraction=0.5,
                          min_category_size=2)
    rows = report["signal_candle_patterns"]["signal:MOMENTUM BUY"]
    assert [r["pattern"] for r in rows][0] == "bullish_marubozu"
    assert {r["pattern"] for r in rows} == {"bullish_marubozu", "shooting_star",
                                            "untagged"}
