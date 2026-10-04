import pytest


@pytest.fixture(autouse=True)
def _isolate_copy_signal_path(tmp_path, monkeypatch):
    """shadow_once always reads AGENT_COPY_SIGNAL_PATH (default
    launch_guard_copy_signals.json, relative to cwd) even for a test that
    only cares about hunter or portfolio. Since copy-v1 joined core_only's
    inputs (it used to be excluded outright), a test run from the repo root
    with the live copy-signal feed actually running would pick up that real,
    live-updated file and unexpectedly ask a fake model for a copy-trader
    proposal it was never written to handle. Any test that wants a specific
    copy-signal fixture overrides this with its own monkeypatch.setenv call
    later in the same test body - last write wins."""
    monkeypatch.setenv(
        "AGENT_COPY_SIGNAL_PATH", str(tmp_path / "unset_copy_signals.json")
    )


@pytest.fixture(autouse=True)
def _no_ambient_paper_min_age(monkeypatch):
    """The live .env turns the paper minimum-token-age guard on, and some
    test imports load .env into the process; tests that care set it
    themselves, the rest must see the code default (off)."""
    monkeypatch.delenv("PAPER_MIN_TOKEN_AGE_MINUTES", raising=False)
    monkeypatch.delenv("MOMENTUM_TAKE_MIN_TOKEN_AGE_MINUTES", raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_hunter_daily_loss_override(monkeypatch):
    """Same reason as above: the live .env sets this, tests that care set it."""
    monkeypatch.delenv("HUNTER_SHADOW_IGNORE_DAILY_LOSS", raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_momentum_take_exits(monkeypatch):
    """momentum-take-v1's exit settings live in the real .env; tests that care
    set them, the rest must see the code defaults."""
    for name in ("MOMENTUM_TAKE_PCT", "MOMENTUM_TAKE_STAGNATION_SECONDS",
                 "MOMENTUM_TAKE_STOP_LOSS_PCT", "MOMENTUM_TAKE_INTERVAL_SECONDS",
                 "MOMENTUM_TAKE_MAX_HOLD_SECONDS", "MOMENTUM_TAKE_YOUNG_MINUTES",
                 "MOMENTUM_TAKE_YOUNG_TAKE_PCT", "MOMENTUM_TAKE_YOUNG_TAKE_MAX_PCT",
                 "MOMENTUM_TAKE_YOUNG_TRAIL_PCT", "MOMENTUM_TAKE_YOUNG_WINDOW_SECONDS",
                 "MOMENTUM_TAKE_YOUNG_PULLBACK_PCT", "MOMENTUM_TAKE_YOUNG_RISE_PCT",
                 "MOMENTUM_TAKE_YOUNG_MAX_HOLD_SECONDS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _isolate_shared_quotes_path(tmp_path, monkeypatch):
    """dexscreener_quotes reads the shared price file in the repo root by
    default; tests must not pick up the live one."""
    monkeypatch.setenv("SHARED_QUOTES_PATH", str(tmp_path / "no_shared_quotes.json"))


@pytest.fixture(autouse=True)
def _no_ambient_rug_block(monkeypatch):
    """The live .env enables the paper rug block; tests that care set it."""
    monkeypatch.delenv("PAPER_RUG_BLOCK_HOURS", raising=False)
    monkeypatch.delenv("PAPER_RUG_LOSS_PCT", raising=False)
    monkeypatch.delenv("PAPER_YOUNG_BUY_MINUTES", raising=False)
    monkeypatch.delenv("PAPER_YOUNG_REBUY_AFTER_MINUTES", raising=False)

