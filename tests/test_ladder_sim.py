import json as jsonlib
import time
from datetime import datetime

import pytest
from solana_launch_guard.eval_cli import _parse_since, build_report, main
from solana_launch_guard.evaluation import (
    CostModel,
    ExitRules,
    LadderRules,
    Observation,
    TrackedDecision,
    simulate_ladder_trade,
    simulate_trade,
)
from solana_launch_guard.outcome_tracker import OutcomeStore

FREE = CostModel(position_usd=5, slippage_bps_per_side=0, fee_bps_per_side=0,
                 fixed_fee_usd_per_side=0)
LIVE = LadderRules(stagnation_enabled=False)


def path(*prices, step=60.0, liquidity=9000.0):
    obs = [Observation(10 + i * step, p, liquidity, p is not None)
           for i, p in enumerate(prices)]
    return TrackedDecision("signal", "M", 0.0, "MOMENTUM BUY", (), tuple(obs))


def test_principal_recovered_at_2x_then_trailing_stop_on_the_rest():
    # 1.0 -> 2.0 (sell half: $5 back) -> 4.0 peak -> 2.8 (30% off peak > 25%)
    result = simulate_ladder_trade(path(1.0, 2.0, 4.0, 2.8), LIVE, FREE)
    assert result.exit_reason.startswith("PRINCIPAL")
    assert "SECOND_STAGE" in result.exit_reason and "TRAILING" in result.exit_reason
    # tokens 5; sold 2.5 @2 = 5; at 4.0 (>=3x) sold half of 2.5 = 1.25 @4 = 5;
    # rest 1.25 @2.8 = 3.5  -> proceeds 13.5 - 5 = 8.5
    assert result.pnl_usd == pytest.approx(8.5)


def test_house_money_is_not_stopped_out_by_the_entry_stop_loss():
    # After principal is back, a fall below entry*(1-stop) is handled only by
    # the widened trailing stop, as in the live exit.
    rules = LadderRules(stagnation_enabled=False, trailing_activation_pct=500)
    result = simulate_ladder_trade(path(1.0, 2.0, 0.5, 0.4), rules, FREE)
    assert "STOP_LOSS" not in result.exit_reason
    assert result.exit_reason == "PRINCIPAL+DATA_END"
    assert result.pnl_usd == pytest.approx(5 + 2.5 * 0.4 - 5)


def test_stop_loss_before_principal():
    result = simulate_ladder_trade(path(1.0, 0.9, 0.7), LIVE, FREE)
    assert result.exit_reason == "STOP_LOSS"
    assert result.pnl_usd == pytest.approx(-1.5)


def test_trailing_uses_the_tighter_stop_before_principal():
    # +30% activates; 1.3 -> 1.14 is 12.3% off the peak: exit
    result = simulate_ladder_trade(path(1.0, 1.3, 1.14), LIVE, FREE)
    assert result.exit_reason == "TRAILING"


def test_stagnation_exit():
    rules = LadderRules(stagnation_window_seconds=300, stagnation_min_gain_pct=3)
    flat = path(1.0, 1.01, 1.0, 1.02, 1.01, 1.0, 1.0)
    result = simulate_ladder_trade(flat, rules, FREE)
    assert result.exit_reason == "STAGNANT"


def test_remainder_dies_after_principal_keeps_the_stake():
    result = simulate_ladder_trade(path(1.0, 2.0, None, None), LIVE, FREE)
    assert result.exit_reason == "PRINCIPAL+DIED"
    assert result.pnl_usd == pytest.approx(0.0)


def test_each_leg_pays_its_own_fees():
    costs = CostModel(position_usd=5, slippage_bps_per_side=0, fee_bps_per_side=0,
                      fixed_fee_usd_per_side=0.1)
    result = simulate_ladder_trade(path(1.0, 2.0, 4.0, 2.8), LIVE, costs)
    free = simulate_ladder_trade(path(1.0, 2.0, 4.0, 2.8), LIVE, FREE)
    assert result.pnl_usd < free.pnl_usd - 0.3  # buy + three sells


def test_ladder_and_simple_exit_disagree_on_a_runner():
    runner = path(1.0, 1.4, 2.0, 3.5, 5.0, 4.0)
    simple = simulate_trade(runner, ExitRules(take_profit_pct=30), FREE)
    ladder = simulate_ladder_trade(runner, LIVE, FREE)
    assert simple.pnl_usd == pytest.approx(1.5)
    assert ladder.pnl_usd > simple.pnl_usd


def test_report_runs_with_the_ladder():
    decisions = [path(1.0, 2.0, 4.0, 2.8)]
    report = build_report(decisions, LIVE, FREE, train_fraction=0.5,
                          min_category_size=1)
    assert report["exit_model"] == "ladder"
    assert report["rules"]["principal_multiple"] == 2.0


def test_ladder_defaults_come_from_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AUTO_SELL_PRINCIPAL_MULTIPLE", "2.5")
    monkeypatch.chdir(tmp_path)
    main(["--outcomes-db", str(tmp_path / "o.db"), "report",
          "--exit-model", "ladder", "--json"])
    assert '"principal_multiple": 2.5' in capsys.readouterr().out


def _store_with(tmp_path, decisions):
    store = OutcomeStore(tmp_path / "o.db")
    with store.connection:
        for i, d in enumerate(decisions):
            store.connection.execute(
                "INSERT INTO tracked_decisions VALUES (?,?,?,?,?,?,?)",
                ("signal", i, d.mint, d.decided_at, d.label, None, "[]"),
            )
            for o in d.observations:
                store.connection.execute(
                    "INSERT INTO observations(mint, observed_at, price_usd, "
                    "liquidity_usd, found) VALUES (?,?,?,?,?)",
                    (d.mint, o.observed_at, o.price_usd, o.liquidity_usd, int(o.found)),
                )
    store.close()


def test_sweep_ranks_on_train_and_reports_test(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    decisions = []
    for i in range(80):
        start = i * 10_000.0
        prices = (1.0, 1.3, 1.6, 1.25, 1.0) if i % 2 else (1.0, 0.85, 0.7, 0.6)
        obs = tuple(Observation(start + 10 + k * 60, p, 9000, True)
                    for k, p in enumerate(prices))
        decisions.append(TrackedDecision("signal", f"m{i}", start, "MOMENTUM BUY",
                                         (), obs))
    _store_with(tmp_path, decisions)
    main(["--outcomes-db", str(tmp_path / "o.db"), "sweep",
          "--stops", "10,30", "--trails", "12", "--activations", "20",
          "--principal-multiples", "2", "--stagnation-windows", "0",
          "--min-train-trades", "10"])
    out = capsys.readouterr().out
    assert "Top settings ranked by TRAIN" in out
    first = out.split("Top settings ranked by TRAIN")[1].splitlines()[1]
    assert first.strip().startswith("10/")  # the tighter stop wins on this data


def test_sweep_on_empty_store_says_not_enough_data(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["--outcomes-db", str(tmp_path / "o.db"), "sweep"])
    assert "Not enough data yet" in capsys.readouterr().out


def _epoch(text):
    return time.mktime(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timetuple())


def test_parse_since_accepts_a_bare_time_or_a_full_datetime():
    fixed_now = datetime(2026, 9, 25)
    assert _parse_since("12:07", now=fixed_now) == _epoch("2026-09-25 12:07:00")
    assert _parse_since("2026-09-24 20:32") == _epoch("2026-09-24 20:32:00")
    assert _parse_since("2026-09-24T20:32:05") == _epoch("2026-09-24 20:32:05")
    with pytest.raises(SystemExit):
        _parse_since("not a time")


def test_since_excludes_decisions_before_the_cutoff(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = TrackedDecision("signal", "OLD", _epoch("2026-09-25 12:00:00"),
                             "MOMENTUM BUY", (), path(1.0, 1.3, 1.1).observations)
    after = TrackedDecision("signal", "NEW", _epoch("2026-09-25 12:10:00"),
                            "MOMENTUM BUY", (), path(1.0, 1.3, 1.1).observations)
    _store_with(tmp_path, [before, after])
    main(["--outcomes-db", str(tmp_path / "o.db"), "report",
          "--since", "2026-09-25 12:07:00", "--min-category-size", "1", "--json"])
    report = jsonlib.loads(capsys.readouterr().out)
    assert report["groups"]["signal:MOMENTUM BUY"]["tracked"] == 1
