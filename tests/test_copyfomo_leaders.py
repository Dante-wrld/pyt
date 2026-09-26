import sqlite3
from datetime import UTC, datetime

import pytest
from solana_launch_guard.config import parse_leader_wallets
from solana_launch_guard.copyfomo_report import (
    WalletTradeRow,
    attribute_leaders,
    build_copyfomo_report,
    per_token,
    render_copyfomo_report,
)
from solana_launch_guard.core import SQLiteStore
from solana_launch_guard.outcome_tracker import (
    OutcomeStore,
    OutcomeTracker,
    TrackerConfig,
)

from test_evaluation import FakeClient, _make_launch_db

T0 = datetime(2026, 9, 26, tzinfo=UTC).timestamp()
A = "A" * 44
B = "B" * 44
CF = "C" * 44


def buy(mint, t, usdc=-5.0, sig=None):
    return WalletTradeRow(
        T0 + t, sig or f"b-{mint}-{t}", mint, mint, "BUY", 100, 0.0, usdc
    )


def sell(mint, t, usdc=5.0, sig=None):
    return WalletTradeRow(
        T0 + t, sig or f"s-{mint}-{t}", mint, mint, "SELL", 100, 0.0, usdc
    )


def test_parse_leader_wallets():
    assert parse_leader_wallets(f"ryantrost:{A}, {B} ,rowdy:{A}") == (
        ("ryantrost", A), (B[:6], B),
    )
    assert parse_leader_wallets("") == ()


def test_copy_is_attributed_by_mint_and_time_not_name():
    copyfomo = per_token([buy("WEED", 60), sell("WEED", 900, usdc=7.0)])
    rows = attribute_leaders(copyfomo, {
        "ryantrost": [buy("WEED", 0, usdc=-1000)],
        "rowdy": [buy("OTHER", 30)],             # different mint: not a match
    })
    [position] = copyfomo
    assert position.leader == "ryantrost" and position.leader_match == "mint+time"
    by_leader = {r["leader"]: r for r in rows}
    assert by_leader["ryantrost"]["copied"] is True
    assert by_leader["rowdy"]["copied"] is False  # rowdy's buy was skipped


def test_a_buy_long_after_the_leader_is_not_a_copy():
    copyfomo = per_token([buy("WEED", 3600)])
    attribute_leaders(copyfomo, {"ryantrost": [buy("WEED", 0)]})
    assert copyfomo[0].leader is None


def test_two_leaders_on_the_same_mint_is_ambiguous_not_guessed():
    copyfomo = per_token([buy("CRACKER", 120)])
    attribute_leaders(copyfomo, {
        "ryantrost": [buy("CRACKER", 0)], "MetaHunter168": [buy("CRACKER", 60)],
    })
    [position] = copyfomo
    assert position.leader is None
    assert position.leader_match == "ambiguous: MetaHunter168, ryantrost"


def test_report_shows_leader_results_and_skips():
    copyfomo = per_token([buy("WEED", 60), sell("WEED", 900, usdc=7.0)])
    rows = attribute_leaders(copyfomo, {
        "ryantrost": [buy("WEED", 0, usdc=-100), sell("WEED", 1000, usdc=150),
                      buy("MISS", 5000, usdc=-100), sell("MISS", 6000, usdc=300)],
    })
    report = build_copyfomo_report(copyfomo, rows)
    assert report["leaders"]["ryantrost (USDC)"]["realized"] == pytest.approx(2.0)
    leader = report["leader_trades"]["ryantrost"]
    assert (leader["copied"], leader["skipped"]) == (1, 1)
    assert leader["skipped_results"]["USDC"]["avg_return"] == pytest.approx(2.0)
    text = render_copyfomo_report(report, CF)
    assert "copied vs skipped" in text and "skipped  closed 1" in text


def test_without_leader_wallets_the_report_says_how_to_add_them():
    report = build_copyfomo_report(per_token([buy("WEED", 0)]))
    assert "COPYFOMO_LEADER_WALLETS" in render_copyfomo_report(report, CF)


def _wallet_db(path, rows):
    _make_launch_db(path, [])
    SQLiteStore(str(path)).close()
    db = sqlite3.connect(path)
    for i, (at, wallet, mint, side) in enumerate(rows):
        db.execute(
            "INSERT INTO wallet_trades(seen_at, wallet, signature, slot, mint, side, "
            "token_delta) VALUES (?, ?, ?, ?, ?, ?, 1)",
            (datetime.fromtimestamp(at, UTC).isoformat(), wallet, f"s{i}", i,
             mint, side),
        )
    db.commit()
    db.close()


def test_tracker_follows_copyfomo_and_leader_buys_once_each(tmp_path):
    launch_db = tmp_path / "launch_guard.db"
    _wallet_db(launch_db, [
        (1000.0, A, "WEED", "BUY"),
        (1030.0, CF, "WEED", "BUY"),   # the copy: same mint, tracked separately
        (1040.0, CF, "WEED", "BUY"),   # a top-up: not a new entry
        (1050.0, CF, "WEED", "SELL"),  # sells are not entries
        (1060.0, "X" * 44, "WEED", "BUY"),  # someone not configured
    ])
    store = OutcomeStore(tmp_path / "o.db")
    tracker = OutcomeTracker(
        store, FakeClient({"WEED": 1.0}), TrackerConfig(),
        launch_db=launch_db, ledger_db=None, clock=lambda: 1070.0,
        wallet_labels={A: "leader:ryantrost", CF: "COPYFOMO"},
    )
    assert tracker.ingest() == 2
    assert sorted(d.label for d in store.load()) == ["COPYFOMO", "leader:ryantrost"]
    assert tracker.ingest() == 0
    store.close()


def _guard(tmp_path, **overrides):
    from solana_launch_guard.app import LaunchGuard

    from test_core import settings

    database = tmp_path / "g.db"
    store = SQLiteStore(str(database))
    return store, LaunchGuard(settings(database, **overrides), store)


def test_leader_evm_wallets_get_one_read_only_watcher_each(tmp_path, monkeypatch):
    import asyncio

    from solana_launch_guard import launch_guard_copyfomo_monitor as monitor

    made = []

    class FakeWatcher:
        def __init__(self, **kwargs):
            made.append(kwargs["wallet"])

        async def run_forever(self):
            return None

    monkeypatch.setattr(monitor, "EvmWalletWatcher", FakeWatcher)
    leaders = (("ryantrost", "0x" + "a" * 40), ("Unipcs", "0x" + "b" * 40))
    store, guard = _guard(
        tmp_path, copyfomo_leader_evm_wallets=leaders,
        base_rpc_url="https://rpc.example", copyfomo_evm_chain="base",
    )

    async def run():
        tasks = guard._leader_evm_tasks(exclude=None)
        await asyncio.gather(*tasks)
        return tasks

    assert len(asyncio.run(run())) == 2
    assert made == [a for _, a in leaders]
    store.close()


def test_leader_evm_recording_needs_an_rpc_url(tmp_path):
    store, guard = _guard(
        tmp_path, copyfomo_leader_evm_wallets=(("x", "0x" + "a" * 40),),
        base_rpc_url="", copyfomo_evm_chain="base",
    )
    assert guard._leader_evm_tasks(exclude=None) == []
    store.close()


def test_bad_evm_leader_address_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="COPYFOMO_LEADER_EVM_WALLETS"):
        _guard(tmp_path, copyfomo_leader_evm_wallets=(("x", "0xnothex"),))
