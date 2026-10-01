"""Price-volume confirmation engine: states, gates, integration, replay."""

import json
import sqlite3
import time

import pytest
from solana_launch_guard.core import SQLiteStore
from solana_launch_guard.evaluation import (
    CostModel,
    LadderRules,
    Observation,
    TrackedDecision,
)
from solana_launch_guard.hunter_shadow_strategy import (
    ShadowRecoveryPolicy,
    assess_entry,
    assess_exit,
)
from solana_launch_guard.market import MarketQuote
from solana_launch_guard.outcome_tracker import read_buy_signals
from solana_launch_guard.price_volume import (
    PriceVolumeConfig,
    Sample,
    allows_entry,
    assess,
    combined_entry_score,
    entry_block_reason,
    explain,
)
from solana_launch_guard.pv_backtest import build, load_samples, parse_score_row, replay
from solana_launch_guard.recommendations import (
    RecommendationBook,
    RecommendationCandidate,
    build_snapshot,
)

CFG = PriceVolumeConfig()
MINT = "P" * 32


def series(points, *, step=30.0, liquidity=50_000.0, start=1_000.0):
    """points: (price, m5 volume, buys, sells[, liquidity])."""
    return [
        Sample(
            at=start + i * step,
            price=p[0],
            volume_m5_usd=p[1],
            buys_m5=p[2],
            sells_m5=p[3],
            liquidity_usd=p[4] if len(p) > 4 else liquidity,
        )
        for i, p in enumerate(points)
    ]


def flat(n, price=1.0, vol=1000.0, buys=20, sells=20):
    return [
        (price * (1.002 if i % 2 else 0.998), vol * (1 + 0.01 * (i % 3)), buys, sells)
        for i in range(n)
    ]


RALLY = [(1.02**i, 1000 + 600 * i, 40, 15) for i in range(1, 13)]
TOP = RALLY[-1][0]


def healthy_pullback():
    dip = [(TOP * (1 - 0.012 * i), 7000 * 0.75**i, 18, 14) for i in range(1, 7)]
    low = dip[-1]
    rebound = [
        (low[0] * (1 + 0.012 * i), low[1] * 1.35**i, 35, 12) for i in range(1, 5)
    ]
    return flat(20) + RALLY + dip + rebound


def dangerous_pullback():
    return (
        flat(20)
        + RALLY
        + [(TOP * (1 - 0.015 * i), 8000 + 1500 * i, 10, 35) for i in range(1, 8)]
    )


def dead_cat():
    drop = [(TOP * (1 - 0.02 * i), 9000 + 800 * i, 8, 40) for i in range(1, 6)]
    low = drop[-1]
    return (
        flat(20)
        + RALLY
        + drop
        + [(low[0] * (1 + 0.012 * i), low[1] * 0.6, 20, 18) for i in range(1, 4)]
    )


def base():
    return [
        (1 + 0.01 * ((i % 4) - 1.5) / 1.5, 1000 * (1 - 0.01 * i), 22, 14)
        for i in range(30)
    ]


SCENARIOS = {
    "bullish continuation": (
        flat(30) + [(1.01**i, 1000 * 1.08**i, 40, 15) for i in range(1, 16)],
        "BULL_CONFIRMED",
        "HOLD",
    ),
    "weakening rally": (
        flat(30, vol=4000) + [(1.01**i, 4000 * 0.93**i, 25, 20) for i in range(1, 16)],
        "BULL_WEAKENING",
        "HOLD_CAUTIOUS",
    ),
    "healthy pullback": (healthy_pullback(), "HEALTHY_DIP_CONFIRMED", "HOLD"),
    "dangerous pullback": (dangerous_pullback(), "BREAKDOWN_RISK", "EXIT_REVIEW"),
    "dead-cat bounce": (dead_cat(), "DEAD_CAT_BOUNCE", "REDUCE"),
    "accumulation": (base(), "ACCUMULATION_CANDIDATE", "HOLD"),
    "accumulation breakout": (
        base() + [(1.03, 2500, 45, 10), (1.05, 3200, 50, 10)],
        "ACCUMULATION_BREAKOUT",
        "HOLD",
    ),
    "distribution": (
        flat(30)
        + [
            (1.0 if i % 2 == 0 else 0.994, 1000 if i % 2 == 0 else 4000, 15, 25)
            for i in range(20)
        ],
        "DISTRIBUTION_CANDIDATE",
        "TAKE_PARTIAL",
    ),
    "bearish breakdown (liquidity)": (
        [
            (1 - 0.005 * i, 2000 + 50 * i, 15, 30, 50_000 * (1 - 0.008 * i))
            for i in range(40)
        ],
        "BREAKDOWN_RISK",
        "EXIT_REVIEW",
    ),
    "bear confirmed": (
        flat(30) + [(0.99**i, 1000 * 1.08**i, 12, 35) for i in range(1, 16)],
        "BEAR_CONFIRMED",
        "REDUCE",
    ),
    "volume climax": (
        flat(40) + [(1.25, 15_000, 80, 30)],
        "VOLUME_SHOCK",
        "HOLD_CAUTIOUS",
    ),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenarios_reach_their_state(name):
    points, state, action = SCENARIOS[name]
    reading = assess(series(points), CFG)
    assert reading.state == state, explain(reading, name)
    assert reading.exit_action == action
    assert "STATE: " + state in explain(reading, name)


def test_only_confirming_states_pass_confirm_mode_and_bad_ones_are_vetoed():
    bull = assess(series(SCENARIOS["bullish continuation"][0]), CFG)
    dip = assess(series(SCENARIOS["healthy pullback"][0]), CFG)
    assert bull.entry_eligible and not bull.vetoed
    assert dip.entry_eligible and not dip.vetoed
    for name in (
        "dangerous pullback",
        "dead-cat bounce",
        "distribution",
        "weakening rally",
        "volume climax",
        "bear confirmed",
    ):
        reading = assess(series(SCENARIOS[name][0]), CFG)
        assert reading.vetoed and not reading.entry_eligible, name


def test_persistence_requires_repeated_confirmation():
    points = SCENARIOS["accumulation breakout"][0]
    reading = assess(series(points), CFG)
    assert reading.state == "ACCUMULATION_BREAKOUT"
    assert reading.confirmations < CFG.confirmations_required
    assert not reading.entry_eligible
    assert any("recent samples confirm" in b for b in reading.blockers)
    loose = PriceVolumeConfig(confirmations_required=1)
    assert assess(series(points), loose).entry_eligible


def test_short_history_is_unknown_and_fails_closed():
    reading = assess(series(flat(4)), CFG)
    assert reading.state == "UNKNOWN" and reading.vetoed
    assert not assess(series(flat(4)), PriceVolumeConfig(block_unknown=False)).vetoed
    assert assess([], CFG).state == "UNKNOWN"


def test_no_look_ahead_later_samples_are_ignored():
    healthy = series(SCENARIOS["healthy pullback"][0])
    crash = series([(0.3, 90_000, 2, 90)] * 5, start=healthy[-1].at + 30)
    cut = healthy[-1].at
    assert assess(healthy + crash, CFG, at=cut).state == assess(healthy, CFG).state


def test_cached_duplicate_polls_do_not_flatten_the_read():
    points = SCENARIOS["bullish continuation"][0]
    doubled = [p for p in points for _ in (0, 1)]
    stretched = series(doubled, step=15.0)
    assert assess(stretched, CFG).state == "BULL_CONFIRMED"


def test_bad_samples_are_skipped():
    rows = series(SCENARIOS["bullish continuation"][0])
    rows.append(Sample(rows[-1].at + 30, float("nan"), 1, 1, 1, 1))
    rows.append(Sample(rows[-1].at + 60, 0.0, 1, 1, 1, 1))
    assert assess(rows, CFG).state == "BULL_CONFIRMED"


def test_suspicious_volume_lowers_quality():
    points = flat(40) + [(1.001, 30_000, 3, 2)]  # spike, few trades, no price move
    reading = assess(series(points), CFG)
    assert reading.quality is not None and reading.quality < CFG.min_quality
    assert reading.vetoed


def test_combined_score_weights_health_and_volume():
    cfg = PriceVolumeConfig(health_weight=3, signal_weight=1)
    assert combined_entry_score(80, 40, cfg) == 70
    assert combined_entry_score(80, None, cfg) == 80


def test_config_from_env_and_validation(monkeypatch):
    monkeypatch.setenv("PV_ENABLED", "true")
    monkeypatch.setenv("PV_MODE", "confirm")
    monkeypatch.setenv("PV_SIGNALS", "buy zone, copy")
    monkeypatch.setenv("PV_CONFIRMATIONS_REQUIRED", "2")
    monkeypatch.setenv("PV_CONFIRMATION_WINDOW", "3")
    cfg = PriceVolumeConfig.from_env()
    assert cfg.enabled and cfg.mode == "confirm"
    assert cfg.signals == {"BUY ZONE", "COPY"}
    assert (cfg.confirmations_required, cfg.confirmation_window) == (2, 3)
    monkeypatch.setenv("PV_CONFIRMATIONS_REQUIRED", "4")
    with pytest.raises(ValueError):
        PriceVolumeConfig.from_env()
    monkeypatch.setenv("PV_CONFIRMATIONS_REQUIRED", "2")
    monkeypatch.setenv("PV_MODE", "yolo")
    with pytest.raises(ValueError):
        PriceVolumeConfig.from_env()


def test_defaults_are_off_and_scoped_to_momentum():
    cfg = PriceVolumeConfig()
    assert not cfg.enabled and not cfg.exit_enabled
    assert cfg.mode == "veto" and cfg.signals == {"MOMENTUM BUY"}


def test_shared_gate():
    on = PriceVolumeConfig(enabled=True)
    vetoed = {
        "pv_state": "BEAR_CONFIRMED",
        "pv_vetoed": True,
        "pv_entry_eligible": False,
    }
    fine = {"pv_state": "BULL_CONFIRMED", "pv_vetoed": False, "pv_entry_eligible": True}
    weak = {"pv_state": "WATCH", "pv_vetoed": False, "pv_entry_eligible": False}
    assert entry_block_reason("MOMENTUM BUY", vetoed, PriceVolumeConfig()) is None
    assert entry_block_reason("BUY ZONE", vetoed, on) is None  # not in scope
    assert "veto" in entry_block_reason("MOMENTUM BUY", vetoed, on)
    assert entry_block_reason("MOMENTUM BUY", fine, on) is None
    assert entry_block_reason("MOMENTUM BUY", weak, on) is None
    confirm = PriceVolumeConfig(enabled=True, mode="confirm")
    assert "does not confirm" in entry_block_reason("MOMENTUM BUY", weak, confirm)
    assert (
        entry_block_reason("MOMENTUM BUY", {}, on) == "price-volume history unavailable"
    )
    assert (
        entry_block_reason(
            "MOMENTUM BUY", {}, PriceVolumeConfig(enabled=True, block_unknown=False)
        )
        is None
    )
    reading = assess(series(SCENARIOS["bear confirmed"][0]), on)
    assert not allows_entry(reading, on)


def hunter_row(**changes):
    row = {
        "mint": MINT,
        "chain": "solana",
        "symbol": "P",
        "decision": "MOMENTUM BUY",
        "price": 1.2,
        "price_currency": "USD",
        "peak_price": 1.2,
        "pullback_from_peak_pct": 0,
        "liquidity_usd": 20_000,
        "initial_liquidity_usd": 20_000,
        "price_change_m5_pct": 3,
        "momentum_label": "RISING",
        "volume_label": "RISING",
        "buys_m5": 60,
        "sells_m5": 20,
        "buy_sell_ratio": 3,
        "risk_label": "MEDIUM",
        "signal_score": 80,
        "entry_confirmation_count": 1,
        "entry_confirmation_required": 1,
        "pv_state": "BEAR_CONFIRMED",
        "pv_vetoed": True,
        "pv_entry_eligible": False,
        "quoted_at": time.time(),
    }
    row.update(changes)
    return row


def test_hunter_gate_off_by_default_and_blocks_when_enabled():
    assert assess_entry(hunter_row(), ShadowRecoveryPolicy())["state"] == "BUY_READY"
    policy = ShadowRecoveryPolicy(price_volume=PriceVolumeConfig(enabled=True))
    review = assess_entry(hunter_row(), policy)
    assert review["state"] != "BUY_READY"
    assert "price_volume" in review["failure_codes"]
    ok = hunter_row(pv_state="BULL_CONFIRMED", pv_vetoed=False)
    assert assess_entry(ok, policy)["state"] == "BUY_READY"


def test_hunter_policy_reads_pv_env(monkeypatch):
    monkeypatch.setenv("PV_ENABLED", "1")
    assert ShadowRecoveryPolicy.from_env().price_volume.enabled


def test_pv_exit_is_opt_in_and_ranks_below_existing_stops():
    position = {
        "entry_price": 1.0,
        "highest_price_since_entry": 1.02,
        "opened_at": time.time() - 30,
    }
    quote = {
        "price": 0.97,
        "liquidity_usd": 20_000,
        "price_change_m5_pct": -1,
        "buys_m5": 10,
        "sells_m5": 12,
        "pv_exit_action": "EXIT_REVIEW",
        "pv_state": "BREAKDOWN_RISK",
        "pv_reason": "liquidity down 20%",
    }
    assert assess_exit(position, quote, ShadowRecoveryPolicy())["state"] != "EXIT"
    policy = ShadowRecoveryPolicy(price_volume=PriceVolumeConfig(exit_enabled=True))
    result = assess_exit(position, quote, policy)
    assert result["state"] == "EXIT"
    assert any("price-volume breakdown" in r for r in result["reasons"])
    stop = assess_exit(position, {**quote, "price": 0.5}, policy)
    assert any("hard stop" in r for r in stop["reasons"])


def board_quote(price, volume, buys=40, sells=15, liquidity=50_000):
    return MarketQuote(
        mint=MINT,
        symbol="P",
        price_sol=price,
        liquidity_usd=liquidity,
        market_cap_usd=None,
        pair_address="pool",
        pair_created_at_ms=1,
        buys_m5=buys,
        sells_m5=sells,
        volume_m5_usd=volume,
        price_change_m5_pct=0,
        price_usd=price,
    )


def candidate():
    return RecommendationCandidate(
        mint=MINT,
        symbol="P",
        chain="solana",
        tier="CORE",
        intelligence_score=80,
        initial_price=1.0,
        current_price=1.0,
        price_currency="USD",
        liquidity_usd=50_000,
        initial_liquidity_usd=50_000,
        volume_m5_usd=1000,
        initial_volume_m5_usd=1000,
        buys_m5=20,
        sells_m5=20,
        price_change_m5_pct=0,
        buy_sell_ratio=1,
        observed_at=0,
        updated_at=0,
    )


def test_board_tracks_pv_and_exposes_it_and_cleans_up():
    book = RecommendationBook()
    c = candidate()
    book.candidates[c.key] = c
    points = SCENARIOS["bullish continuation"][0]
    for i, (price, vol, buys, sells) in enumerate(points):
        book.update(board_quote(price, vol, buys, sells), now=1000 + 30 * i)
    assert c.pv_state == "BULL_CONFIRMED" and not c.pv_vetoed
    assert c.pv_reading["pv_state"] == "BULL_CONFIRMED"
    row = build_snapshot([c], pending_count=0, poll_seconds=20)["candidates"][0]
    assert row["pv_state"] == "BULL_CONFIRMED" and row["pv_exit_action"] == "HOLD"
    assert book.price_volume(c.key).state == "BULL_CONFIRMED"
    drained = book.drain_pv_samples()
    assert len(drained) == len(points) and book.drain_pv_samples() == []
    book.expire(now=10**10)
    assert c.key not in book.pv_history


def test_board_samples_and_signal_tag_reach_evaluation(tmp_path):
    database = tmp_path / "g.db"
    store = SQLiteStore(str(database))
    store.save_board_samples([(1000.0, "solana", MINT, 1.0, 500.0, 5, 4, 20_000.0)])
    store.save_buy_signal(
        mint=MINT,
        symbol="P",
        chain="solana",
        decision="MOMENTUM BUY",
        price=1.0,
        price_currency="USD",
        liquidity_usd=20_000,
        signal_score=80,
        pair_created_at_ms=1,
        reason="r",
        live_blocked_reason=None,
        sources="board",
        pv_state="BULL_CONFIRMED",
    )
    [decision] = read_buy_signals(database, 0)
    assert "pv: BULL_CONFIRMED" in decision.reasons
    samples = load_samples(database, {MINT})
    assert samples[MINT][0].price == 1.0


def test_old_buy_signals_tables_gain_the_column(tmp_path):
    database = tmp_path / "old.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE buy_signals (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "signaled_at TEXT NOT NULL, mint TEXT NOT NULL, symbol TEXT NOT NULL, "
        "chain TEXT NOT NULL, decision TEXT NOT NULL, price REAL, "
        "price_currency TEXT, liquidity_usd REAL, signal_score INTEGER, "
        "pair_created_at_ms INTEGER, reason TEXT, live_blocked_reason TEXT)"
    )
    connection.commit()
    connection.close()
    store = SQLiteStore(str(database))
    columns = {r[1] for r in store.connection.execute("PRAGMA table_info(buy_signals)")}
    assert {"pv_state", "pullback_volume"} <= columns


def test_score_rows_parse_into_samples():
    reasons = json.dumps(
        [
            "liquidity=$89,633, market_cap=$387,908, buys/sells=17/25, volume5m=$856",
            "negative five-minute momentum",
        ]
    )
    sample = parse_score_row("2026-10-01T15:00:59+00:00", reasons)
    assert sample is not None
    assert (sample.price, sample.volume_m5_usd, sample.buys_m5, sample.sells_m5) == (
        387_908,
        856,
        17,
        25,
    )
    zero = reasons.replace("387,908", "0")
    assert parse_score_row("2026-10-01T15:00:59+00:00", zero) is None
    assert parse_score_row("2026-10-01T15:00:59+00:00", "[]") is None


def test_replay_uses_only_history_before_each_signal_and_compares_paths():
    healthy = series(SCENARIOS["healthy pullback"][0], start=0)
    bear = series(SCENARIOS["bear confirmed"][0], start=0)
    signal_at = max(healthy[-1].at, bear[-1].at) + 10
    future_crash = series([(0.01, 1, 1, 1)] * 3, start=signal_at + 5)

    def path(start, gains):
        return tuple(
            Observation(signal_at + 60 * i, start * g, 20_000, True)
            for i, g in enumerate(gains)
        )

    good = TrackedDecision(
        "signal",
        "GOOD",
        signal_at,
        "MOMENTUM BUY",
        (),
        path(1.0, [1.0, 1.5, 2.5, 3.5, 3.5]),
    )
    bad = TrackedDecision(
        "signal",
        "BAD",
        signal_at,
        "MOMENTUM BUY",
        (),
        path(1.0, [1.0, 0.9, 0.7, 0.6, 0.6]),
    )
    samples = {"GOOD": healthy + future_crash, "BAD": bear}
    rows = replay([good, bad], samples, CFG, LadderRules(), CostModel(), exits=True)
    by_mint = {r.decision.mint: r for r in rows}
    assert by_mint["GOOD"].state == "HEALTHY_DIP_CONFIRMED"
    assert by_mint["BAD"].state == "BEAR_CONFIRMED" and by_mint["BAD"].vetoed
    report = build(rows, train_fraction=0.5)
    section = report["signals"]["MOMENTUM BUY"]["all"]
    assert section["current"]["trades"] == 2
    assert section["pv_veto_mode"]["trades"] == 1
    assert (
        section["pv_veto_mode"]["expectancy_usd"] > section["current"]["expectancy_usd"]
    )
    assert report["coverage"]["MOMENTUM BUY"]["with_volume_history"] == 2


def test_replay_marks_signals_without_recent_history():
    stale = series(flat(30), start=0)
    late = stale[-1].at + 3600
    decision = TrackedDecision(
        "signal", "X", late, "BUY NOW", (), (Observation(late, 1.0, 20_000, True),)
    )
    [row] = replay(
        [decision], {"X": stale}, CFG, LadderRules(), CostModel(), exits=False
    )
    assert row.state == "NO_DATA"
