import pytest
from solana_launch_guard.eval_cli import build_report, main
from solana_launch_guard.evaluation import (
    CostModel,
    ExitRules,
    Observation,
    TrackedDecision,
    excursion_stats,
)


def path(mint, *prices):
    obs = tuple(Observation(10 + i * 60, p, 9000, True) for i, p in enumerate(prices))
    return TrackedDecision("signal", mint, 0.0, "MOMENTUM BUY", (), obs)


def test_a_tiny_uptick_is_not_counted_as_real_upside():
    stats = excursion_stats(
        [path("a", 1.0, 1.01, 0.9)], ExitRules(), breakeven_pct=7.4, stop_pct=20
    )
    assert stats["median_peak_pct"] == pytest.approx(1.0)
    assert stats["targets"][0]["reached"] == 0  # never cleared break-even


def test_order_against_the_stop_matters():
    stopped_first = path("a", 1.0, 0.75, 1.6)   # -25% first, then +60%
    target_first = path("b", 1.0, 1.6, 0.75)    # +60% first
    stats = excursion_stats(
        [stopped_first, target_first], ExitRules(), breakeven_pct=7.4, stop_pct=20
    )
    fifty = next(t for t in stats["targets"] if t["pct"] == 50)
    assert fifty["reached"] == 1.0
    assert fifty["reached_before_stop"] == 0.5
    assert stats["share_hit_stop"] == 1.0


def test_peak_timing():
    stats = excursion_stats(
        [path("a", 1.0, 1.1, 1.3, 1.2)], ExitRules(), breakeven_pct=7.4, stop_pct=20
    )
    assert stats["median_minutes_to_peak"] == pytest.approx(2.0)


def test_report_shows_the_upside_line(tmp_path, capsys):
    report = build_report(
        [path("a", 1.0, 1.3, 1.1), path("b", 1.0, 0.7)], ExitRules(), CostModel(),
        train_fraction=0.5, min_category_size=1,
    )
    exc = report["groups"]["signal:MOMENTUM BUY"]["excursions"]
    assert exc["tokens"] == 2
    assert exc["breakeven_pct"] == pytest.approx(CostModel().breakeven_move_pct())


def test_rendered_report_includes_upside(tmp_path, capsys, monkeypatch):
    from solana_launch_guard import eval_cli
    from solana_launch_guard.outcome_tracker import OutcomeStore

    monkeypatch.setattr(eval_cli, "_load_dotenv", lambda *a, **k: None)
    store = OutcomeStore(tmp_path / "o.db")
    with store.connection:
        store.connection.execute(
            "INSERT INTO tracked_decisions VALUES "
            "('signal', 1, 'a', 0.0, 'MOMENTUM BUY', NULL, '[]')"
        )
        for i, p in enumerate((1.0, 1.3, 1.1)):
            store.connection.execute(
                "INSERT INTO observations(mint, observed_at, price_usd, "
                "liquidity_usd, found) VALUES ('a', ?, ?, 9000, 1)",
                (10 + i * 60, p),
            )
    store.close()
    main(["--outcomes-db", str(tmp_path / "o.db"), "report"])
    out = capsys.readouterr().out
    assert "upside reached: break-even +7.4% 100%" in out
    assert "median peak +30% after 1 min" in out
