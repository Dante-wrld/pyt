import asyncio
import sqlite3
from datetime import UTC, datetime

from solana_launch_guard.app import LaunchGuard
from solana_launch_guard.core import SQLiteStore
from solana_launch_guard.eval_cli import _promotion_verdict, build_report, main
from solana_launch_guard.evaluation import (
    CostModel,
    ExitRules,
    Observation,
    Summary,
    TrackedDecision,
)
from solana_launch_guard.outcome_tracker import (
    OutcomeStore,
    OutcomeTracker,
    TrackerConfig,
)
from solana_launch_guard.recommendations import RecommendationCandidate
from solana_launch_guard.strategy_profile import StrategyProfile

from test_core import settings
from test_evaluation import FakeClient, _make_launch_db


def candidate(mint, decision):
    c = RecommendationCandidate(
        mint=mint, symbol=mint, chain="solana", tier="CORE",
        intelligence_score=80, initial_price=1, current_price=1.2,
        price_currency="USD", liquidity_usd=90_000, initial_liquidity_usd=80_000,
        volume_m5_usd=1, initial_volume_m5_usd=1, buys_m5=1, sells_m5=1,
        price_change_m5_pct=0, buy_sell_ratio=1, observed_at=0, updated_at=0,
    )
    c.decision = decision
    c.pair_created_at_ms = 1
    return c


def guard_with(tmp_path, profile):
    database = tmp_path / "g.db"
    store = SQLiteStore(str(database))
    guard = LaunchGuard(settings(database), store)
    guard.strategy_profile = profile
    return store, guard


def rows(store):
    return store.connection.execute(
        "SELECT mint, decision, live_blocked_reason FROM buy_signals ORDER BY id"
    ).fetchall()


def test_logs_each_move_into_a_buy_decision_once(tmp_path):
    store, guard = guard_with(tmp_path, StrategyProfile())
    board = guard.recommendations.candidates
    board["A"] = candidate("A", "WATCH")
    guard._record_buy_signals()
    assert rows(store) == []

    board["A"].decision = "MOMENTUM BUY"
    guard._record_buy_signals()
    guard._record_buy_signals()  # still MOMENTUM BUY: no new row
    board["A"].decision = "BUY ZONE"  # a different buy decision: new row
    guard._record_buy_signals()
    board["A"].decision = "WATCH"
    guard._record_buy_signals()
    board["A"].decision = "BUY ZONE"  # re-entry after leaving: new row
    guard._record_buy_signals()
    assert [(r[0], r[1]) for r in rows(store)] == [
        ("A", "MOMENTUM BUY"), ("A", "BUY ZONE"), ("A", "BUY ZONE"),
    ]
    store.close()


def test_records_whether_the_live_gate_would_have_blocked_it(tmp_path):
    store, guard = guard_with(
        tmp_path, StrategyProfile(entry_allowed_decisions=("BUY ZONE",))
    )
    guard.recommendations.candidates["M"] = candidate("M", "MOMENTUM BUY")
    guard.recommendations.candidates["Z"] = candidate("Z", "BUY ZONE")
    guard._record_buy_signals()
    blocked = {r[0]: r[2] for r in rows(store)}
    assert "shadow-only" in blocked["M"]
    assert blocked["Z"] is None
    store.close()


def test_a_storage_failure_never_breaks_the_monitor(tmp_path):
    store, guard = guard_with(tmp_path, StrategyProfile())
    guard.recommendations.candidates["A"] = candidate("A", "BUY ZONE")
    store.connection.execute("DROP TABLE buy_signals")
    guard._record_buy_signals()  # logs a warning, does not raise
    store.close()


def _signal_db(path, signals):
    _make_launch_db(path, [])
    SQLiteStore(str(path)).close()  # create buy_signals with the real schema
    db = sqlite3.connect(path)
    for at, mint, decision, chain in signals:
        db.execute(
            "INSERT INTO buy_signals(signaled_at, mint, symbol, chain, decision) "
            "VALUES (?, ?, ?, ?, ?)",
            (datetime.fromtimestamp(at, UTC).isoformat(), mint, mint, chain, decision),
        )
    db.commit()
    db.close()


def test_tracker_samples_each_signal_from_its_own_time(tmp_path):
    launch_db = tmp_path / "launch_guard.db"
    _signal_db(launch_db, [(1000.0, "A", "BUY ZONE", "solana"),
                           (1000.0, "EVM", "BUY ZONE", "base")])
    clock = [1010.0]
    store = OutcomeStore(tmp_path / "o.db")
    tracker = OutcomeTracker(
        store, FakeClient({"A": 1.0}), TrackerConfig(),
        launch_db=launch_db, ledger_db=None, clock=lambda: clock[0],
    )
    assert tracker.ingest() == 1  # the non-Solana signal is skipped
    asyncio.run(tracker.sample_once())

    # 40 minutes later the same mint fires MOMENTUM BUY: it must get its own
    # immediate sample even though the mint is already tracked.
    db = sqlite3.connect(launch_db)
    db.execute(
        "INSERT INTO buy_signals(signaled_at, mint, symbol, chain, decision) "
        "VALUES (?, 'A', 'A', 'solana', 'MOMENTUM BUY')",
        (datetime.fromtimestamp(3400.0, UTC).isoformat(),),
    )
    db.commit()
    db.close()
    clock[0] = 3405.0
    assert tracker.ingest() == 1
    assert "A" in store.due_mints(3405.0, 10)
    labels = {d.label for d in store.load()}
    assert labels == {"BUY ZONE", "MOMENTUM BUY"}
    store.close()


def _signals(label, pnl_prices, start):
    return [
        TrackedDecision(
            "signal", f"{label}{i}", start + i, label, (),
            (Observation(start + i + 5, 1.0, 9000, True),
             Observation(start + i + 65, p, 9000, True)),
        )
        for i, p in enumerate(pnl_prices)
    ]


NO_COSTS = CostModel(position_usd=5, slippage_bps_per_side=0, fee_bps_per_side=0,
                     fixed_fee_usd_per_side=0)


def test_report_puts_the_two_signals_head_to_head():
    decisions = _signals("MOMENTUM BUY", [1.3, 0.5] * 5, 0) + _signals(
        "BUY ZONE", [1.3] * 10, 100
    )
    report = build_report(decisions, ExitRules(), NO_COSTS,
                          train_fraction=0.5, min_category_size=1)
    h2h = report["momentum_vs_buy_zone"]
    assert h2h["momentum_test"]["trades"] == 5
    assert "need 100+" in h2h["verdict"]


def test_promotion_rule():
    def s(n, mean, ci):
        return Summary(trades=n, expectancy_usd=mean, expectancy_ci95_usd=ci)
    good_zone = s(200, 0.2, (0.1, 0.3))
    assert "need 100+" in _promotion_verdict(s(50, 1, (0.5, 1.5)), good_zone)
    assert "not above zero" in _promotion_verdict(s(150, 0.1, (-0.1, 0.3)), good_zone)
    worse = s(150, 0.1, (0.05, 0.2))
    assert "worse than BUY ZONE" in _promotion_verdict(worse, good_zone)
    assert "meets the promotion rule" in _promotion_verdict(
        s(150, 0.3, (0.1, 0.5)), good_zone
    )


def test_group_filter(tmp_path, capsys):
    store = OutcomeStore(tmp_path / "o.db")
    store.close()
    main(["--outcomes-db", str(tmp_path / "o.db"), "report", "--group", "signal:"])
    assert "No tracked decisions" in capsys.readouterr().out
