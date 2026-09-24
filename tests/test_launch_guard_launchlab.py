"""price_move_pct: the pure trim-decision helper behind LaunchLab's
discover-then-confirm funnel (see launch_guard_launchlab.py's module
docstring)."""
import pytest

from solana_launch_guard.launch_guard_launchlab import price_move_pct


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
