import asyncio
import json
import sqlite3
from datetime import UTC, datetime

import pytest
from solana_launch_guard.eval_cli import build_report, main
from solana_launch_guard.evaluation import (
    CostModel,
    ExitRules,
    Observation,
    TrackedDecision,
    horizon_stats,
    reason_category,
    simulate_trade,
    summarize,
    time_split,
)
from solana_launch_guard.outcome_tracker import (
    OutcomeStore,
    OutcomeTracker,
    Quote,
    TrackerConfig,
    parse_batch,
    sampled,
)

NO_COSTS = CostModel(
    position_usd=5, slippage_bps_per_side=0, fee_bps_per_side=0,
    fixed_fee_usd_per_side=0,
)
RULES = ExitRules(
    take_profit_pct=30, stop_loss_pct=20, max_hold_seconds=3600,
    min_exit_liquidity_usd=1000,
)


def obs(t, price, liquidity=5000.0, found=True):
    return Observation(t, price, liquidity, found)


def decision(prices, *, label="ACCEPTED", reasons=(), at=0.0, mint="M"):
    return TrackedDecision("launch", mint, at, label, tuple(reasons), tuple(prices))


# --- cost model -----------------------------------------------------------

def test_no_costs_round_trip_is_exact():
    assert NO_COSTS.net_pnl(1.0, 1.0) == pytest.approx(0.0)
    assert NO_COSTS.net_pnl(1.0, 2.0) == pytest.approx(5.0)
    assert NO_COSTS.breakeven_move_pct() == pytest.approx(0.0)


def test_default_costs_need_a_real_move_to_break_even():
    costs = CostModel()
    move = costs.breakeven_move_pct()
    assert 6 < move < 8  # ~3.1% per side twice, plus $0.04 on $5
    exit_price = 1 + move / 100
    assert costs.net_pnl(1.0, exit_price) == pytest.approx(0.0, abs=1e-9)


def test_worthless_exit_loses_whole_stake_not_more():
    assert CostModel().net_pnl(1.0, 0.0) == pytest.approx(-5.0)


# --- trade simulation -----------------------------------------------------

def test_take_profit_fills_at_target_not_at_a_gapped_high():
    result = simulate_trade(decision([obs(10, 1.0), obs(70, 3.0)]), RULES, NO_COSTS)
    assert result.exit_reason == "TAKE_PROFIT"
    assert result.exit_price == pytest.approx(1.3)


def test_stop_loss_fills_at_the_gapped_low():
    result = simulate_trade(decision([obs(10, 1.0), obs(70, 0.4)]), RULES, NO_COSTS)
    assert result.exit_reason == "STOP_LOSS"
    assert result.exit_price == pytest.approx(0.4)
    assert result.pnl_usd == pytest.approx(-3.0)


def test_time_exit_after_max_hold():
    path = [obs(10, 1.0), obs(1000, 1.05), obs(3700, 1.1)]
    result = simulate_trade(decision(path), RULES, NO_COSTS)
    assert result.exit_reason == "TIME_EXIT"
    assert result.exit_price == pytest.approx(1.1)


def test_token_that_vanishes_for_good_counts_as_total_loss():
    path = [obs(10, 1.0), obs(70, 1.1), obs(130, None, None, found=False)]
    result = simulate_trade(decision(path), RULES, NO_COSTS)
    assert result.exit_reason == "DIED"
    assert result.pnl_usd == pytest.approx(-5.0)


def test_liquidity_pulled_counts_as_dead_even_with_a_price():
    path = [obs(10, 1.0), obs(70, 1.1, liquidity=5.0)]
    result = simulate_trade(decision(path), RULES, NO_COSTS)
    assert result.exit_reason == "DIED"


def test_brief_quote_outage_that_recovers_is_not_death():
    path = [obs(10, 1.0), obs(70, None, None, found=False), obs(130, 1.4)]
    result = simulate_trade(decision(path), RULES, NO_COSTS)
    assert result.exit_reason == "TAKE_PROFIT"


def test_no_entry_when_first_usable_quote_is_too_late():
    path = [obs(10, None, None, found=False), obs(900, 1.0), obs(960, 2.0)]
    assert simulate_trade(decision(path), RULES, NO_COSTS) is None


def test_data_end_marks_to_last_price():
    result = simulate_trade(decision([obs(10, 1.0), obs(70, 1.1)]), RULES, NO_COSTS)
    assert result.exit_reason == "DATA_END"
    assert result.exit_price == pytest.approx(1.1)


# --- summaries --------------------------------------------------------------

def test_summary_expectancy_drawdown_and_ci():
    decisions = [
        decision([obs(t, 1.0), obs(t + 60, p)], mint=f"m{t}", at=t - 10)
        for t, p in [(10, 1.3), (100, 0.5), (200, 1.3), (300, 0.5)]
    ]
    results = [simulate_trade(d, RULES, NO_COSTS) for d in decisions]
    summary = summarize(results)
    assert summary.trades == 4
    assert summary.win_rate == 0.5
    assert summary.expectancy_usd == pytest.approx((1.5 - 2.5) / 2)
    assert summary.max_drawdown_usd == pytest.approx(3.5)  # +1.5 peak to -2
    low, high = summary.expectancy_ci95_usd
    assert low <= summary.expectancy_usd <= high


def test_time_split_keeps_order():
    ds = [decision([], at=t, mint=str(t)) for t in (5, 1, 3, 2, 4)]
    train, test = time_split(ds, 0.6)
    assert [d.decided_at for d in train] == [1, 2, 3]
    assert [d.decided_at for d in test] == [4, 5]


def test_reason_category_groups_numbers():
    assert reason_category("market cap 3.2 SOL below minimum 5") == reason_category(
        "market cap 41 SOL below minimum 5.5"
    )


def test_horizon_stats_counts_dead_and_doubles():
    alive = decision([obs(10, 1.0), obs(200, 2.5), obs(320, 1.5)], mint="a")
    dead = decision([obs(10, 1.0), obs(320, None, None, found=False)], mint="b")
    stats = horizon_stats([alive, dead], 300, RULES)
    assert stats.tokens == 2
    assert stats.share_dead == 0.5
    assert stats.share_doubled_by_then == 0.5


# --- tracker ----------------------------------------------------------------

def test_sampling_is_deterministic_and_roughly_the_rate():
    mints = [f"mint{i}" for i in range(4000)]
    picked = [m for m in mints if sampled(m, 0.1)]
    assert picked == [m for m in mints if sampled(m, 0.1)]
    assert 300 < len(picked) < 500


def test_parse_batch_picks_deepest_pool_and_marks_missing():
    payload = [
        {"chainId": "solana", "baseToken": {"address": "A"}, "priceUsd": "1.0",
         "liquidity": {"usd": 100}},
        {"chainId": "solana", "baseToken": {"address": "A"}, "priceUsd": "1.2",
         "liquidity": {"usd": 9000}},
        {"chainId": "base", "baseToken": {"address": "B"}, "priceUsd": "5"},
    ]
    quotes = parse_batch(payload, ["A", "B"])
    assert quotes["A"].price_usd == pytest.approx(1.2)
    assert quotes["B"].price_usd is None


class FakeClient:
    def __init__(self, prices):
        self.prices = prices
        self.calls = 0

    async def quotes(self, mints):
        self.calls += 1
        return {
            m: Quote(m, self.prices.get(m), 5000.0 if m in self.prices else None)
            for m in mints
        }


def _make_launch_db(path, rows):
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "decided_at TEXT, mint TEXT, symbol TEXT, accepted INTEGER, score INTEGER, "
        "reasons_json TEXT)"
    )
    for at, mint, accepted, reasons in rows:
        db.execute(
            "INSERT INTO decisions(decided_at, mint, symbol, accepted, score, "
            "reasons_json) VALUES (?, ?, 'X', ?, 50, ?)",
            (datetime.fromtimestamp(at, UTC).isoformat(), mint,
             accepted, json.dumps(reasons)),
        )
    db.commit()
    db.close()


def test_tracker_never_writes_to_the_bot_database(tmp_path):
    launch_db = tmp_path / "launch_guard.db"
    _make_launch_db(launch_db, [(1000.0, "A", 1, [])])
    before = launch_db.read_bytes()
    clock = [1010.0]
    store = OutcomeStore(tmp_path / "outcomes.db")
    tracker = OutcomeTracker(
        store, FakeClient({"A": 1.0}), TrackerConfig(),
        launch_db=launch_db, ledger_db=tmp_path / "missing.sqlite",
        clock=lambda: clock[0],
    )
    assert tracker.ingest() == 1
    asyncio.run(tracker.sample_once())
    assert launch_db.read_bytes() == before
    store.close()


def test_tracker_end_to_end_report(tmp_path):
    launch_db = tmp_path / "launch_guard.db"
    rows = [(1000.0 + i, f"win{i}", 1, []) for i in range(3)]
    rows += [(1000.0 + i, f"rej{i}", 0, [f"market cap {i} SOL below minimum 5"])
             for i in range(3)]
    rows.append((1.0, "stale", 1, []))  # decided long before tracker started
    _make_launch_db(launch_db, rows)

    prices = {m: 1.0 for _, m, _, _ in rows}
    client = FakeClient(prices)
    clock = [1010.0]
    store = OutcomeStore(tmp_path / "outcomes.db")
    tracker = OutcomeTracker(
        store, client,
        TrackerConfig(reject_sample_rate=1.0, dense_interval_seconds=60,
                      dense_window_seconds=120, horizons_seconds=(300.0,)),
        launch_db=launch_db, ledger_db=None, clock=lambda: clock[0],
    )
    assert tracker.ingest() == 6  # stale one skipped
    assert tracker.ingest() == 0  # cursor prevents double tracking
    asyncio.run(tracker.sample_once())

    for mint in prices:  # winners pump, rejected tokens die
        prices[mint] = 1.5 if mint.startswith("win") else None
    clock[0] = 1400.0
    asyncio.run(tracker.sample_once())

    decisions = store.load()
    assert {d.mint for d in decisions} == {f"win{i}" for i in range(3)} | {
        f"rej{i}" for i in range(3)
    }
    assert store.pending_count() == 0

    report = build_report(
        decisions, RULES, NO_COSTS, train_fraction=0.5, min_category_size=1
    )
    accepted = report["groups"]["launch:ACCEPTED"]["all"]
    rejected = report["groups"]["launch:REJECTED"]["all"]
    assert accepted["win_rate"] == 1.0
    assert rejected["exit_reasons"] == {"DIED": 3}
    [market_cap_filter] = report["rejection_filters"]
    assert market_cap_filter["per_trade_vs_accepted_usd"] < 0
    store.close()


def test_report_cli_runs_on_empty_store(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("solana_launch_guard.eval_cli._load_dotenv", lambda: None)
    main(["--outcomes-db", str(tmp_path / "o.db"), "report"])
    out = capsys.readouterr().out
    assert "Break-even" in out and "No tracked decisions" in out


def test_tracker_reads_board_candidates_from_intelligence_scores(tmp_path):
    launch_db = tmp_path / "launch_guard.db"
    _make_launch_db(launch_db, [])
    db = sqlite3.connect(launch_db)
    db.execute(
        "CREATE TABLE intelligence_scores (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "scored_at TEXT, mint TEXT, symbol TEXT, tier TEXT, total_score INTEGER, "
        "safety_score INTEGER, momentum_score INTEGER, reasons_json TEXT)"
    )
    for _ in range(3):  # the momentum feed re-saves the same mint every poll
        db.execute(
            "INSERT INTO intelligence_scores(scored_at, mint, symbol, tier, "
            "total_score, safety_score, momentum_score, reasons_json) "
            "VALUES (?, 'OLD', 'OLD', 'CORE', 80, 40, 40, '[]')",
            (datetime.fromtimestamp(1000.0, UTC).isoformat(),),
        )
    db.commit()
    db.close()
    store = OutcomeStore(tmp_path / "outcomes.db")
    tracker = OutcomeTracker(
        store, FakeClient({"OLD": 1.0}), TrackerConfig(),
        launch_db=launch_db, ledger_db=None, clock=lambda: 1010.0,
    )
    assert tracker.ingest() == 1
    assert store.due_mints(1010.0, 10) == ["OLD"]
    store.close()
