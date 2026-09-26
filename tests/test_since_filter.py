import time
from datetime import UTC, datetime

import pytest
from solana_launch_guard.eval_cli import main, parse_since
from solana_launch_guard.evaluation import Observation, TrackedDecision

from test_ladder_sim import _store_with


@pytest.fixture
def phoenix(monkeypatch):
    monkeypatch.setenv("TZ", "America/Phoenix")  # UTC-7, no DST
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


# 2026-09-25 15:00 in Phoenix
NOW = datetime(2026, 9, 25, 22, 0, tzinfo=UTC).timestamp()


def test_bare_time_is_today_local(phoenix):
    expected = datetime(2026, 9, 25, 19, 7, tzinfo=UTC).timestamp()  # 12:07 MST
    assert parse_since("12:07", now=NOW) == expected


def test_bare_time_later_than_now_means_yesterday(phoenix):
    expected = datetime(2026, 9, 25, 3, 32, tzinfo=UTC).timestamp()  # 20:32 on 9/24
    assert parse_since("20:32", now=NOW) == expected


def test_dates_offsets_and_epochs(phoenix):
    assert parse_since("2026-09-25 12:07", now=NOW) == parse_since("12:07", now=NOW)
    assert parse_since("2026-09-25T19:07:00+00:00") == parse_since("12:07", now=NOW)
    assert parse_since("1790307124") == 1790307124.0


def test_bad_input_explains_the_formats():
    with pytest.raises(SystemExit, match="HH:MM"):
        parse_since("noonish")


def _signals(times):
    out = []
    for i, at in enumerate(times):
        obs = (Observation(at + 5, 1.0, 9000, True),
               Observation(at + 65, 1.3, 9000, True))
        out.append(TrackedDecision("signal", f"m{i}", at, "MOMENTUM BUY", (), obs))
    return out


def test_report_and_sweep_only_use_decisions_since(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _store_with(tmp_path, _signals([1000.0, 2000.0, 5000.0, 6000.0]))
    db = str(tmp_path / "o.db")

    main(["--outcomes-db", db, "report", "--since", "4000", "--json"])
    out = capsys.readouterr().out
    assert '"tracked": 2' in out and '"since": 4000.0' in out

    main(["--outcomes-db", db, "sweep", "--since", "4000", "--min-train-trades", "1",
          "--stops", "20", "--trails", "12", "--activations", "20",
          "--principal-multiples", "2", "--stagnation-windows", "0"])
    out = capsys.readouterr().out
    assert out.startswith("Only decisions since")
    assert "2 tracked" in out
