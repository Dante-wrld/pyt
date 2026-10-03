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
