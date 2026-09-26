import asyncio
import json
import sqlite3
from datetime import UTC, datetime

import pytest
from solana_launch_guard.app import LaunchGuard
from solana_launch_guard.core import SQLiteStore
from solana_launch_guard.eval_cli import build_report
from solana_launch_guard.evaluation import (
    CostModel,
    ExitRules,
    Observation,
    TrackedDecision,
)
from solana_launch_guard.launch_guard_leader_holdings import (
    SOURCE,
    LeaderHoldingsConfig,
    select_leader_holdings,
)
from solana_launch_guard.market import MarketQuote
from solana_launch_guard.outcome_tracker import (
    OutcomeStore,
    OutcomeTracker,
    TrackerConfig,
)
from solana_launch_guard.recommendations import build_snapshot
from solana_launch_guard.strategy_profile import StrategyProfile
from solana_launch_guard.wallet import SolanaTokenHolding, WalletTrade

from test_buy_signals import candidate, guard_with
from test_core import settings
from test_evaluation import FakeClient, _make_launch_db

A = "A" * 44
B = "B" * 44
CONFIG = LeaderHoldingsConfig(min_holding_usd=100, min_liquidity_usd=50_000,
                              max_candidates=2)


def quote(mint, *, price=1.0, liquidity=80_000.0, symbol=None):
    return MarketQuote(
        mint=mint, symbol=symbol or mint[:4], price_sol=price / 150, price_usd=price,
        liquidity_usd=liquidity, market_cap_usd=200_000, pair_address="P" + mint[:4],
        pair_created_at_ms=1, buys_m5=60, sells_m5=20, volume_m5_usd=15_000,
        price_change_m5_pct=5,
    )


def held(mint, amount):
    return SolanaTokenHolding(mint=mint, amount=amount)


def test_selection_drops_dust_thin_pools_and_stocks_and_ranks_by_value():
    holdings = {
        "ryantrost": [held("BIG", 10_000), held("DUST", 5), held("THIN", 10_000),
                      held("TSLAX", 10_000)],
        "Rowdy": [held("BIG", 5_000), held("MID", 2_000), held("SMALL", 300)],
    }
    quotes = {
        "BIG": quote("BIG"), "DUST": quote("DUST"), "MID": quote("MID"),
        "SMALL": quote("SMALL"), "THIN": quote("THIN", liquidity=1_000),
        "TSLAX": quote("TSLAX", symbol="TSLAx"),
    }
    chosen = select_leader_holdings(
        holdings, quotes, CONFIG, stock_symbols=frozenset({"tsla"})  # stored casefolded
    )
    assert [h.mint for h in chosen] == ["BIG", "MID"]  # capped at 2
    assert chosen[0].leaders == ("Rowdy", "ryantrost")
    assert chosen[0].value_usd == pytest.approx(15_000)


def test_unpriced_holdings_are_ignored():
    chosen = select_leader_holdings({"x": [held("NOQ", 1e9)]}, {"NOQ": None}, CONFIG)
    assert chosen == []


class FakeRpc:
    def __init__(self, by_address):
        self.by_address = by_address

    async def token_holdings(self, owner):
        return tuple(self.by_address[owner])


class FakeOracle:
    def __init__(self, quotes):
        self.quotes = quotes

    async def quote_many(self, mints, *, chain="solana"):
        return {m: self.quotes.get(m) for m in mints}

    async def robinhood_stock_token_symbols(self):
        return frozenset()


def _guard(tmp_path, **overrides):
    database = tmp_path / "g.db"
    store = SQLiteStore(str(database))
    return store, LaunchGuard(settings(database, **overrides), store)


def test_poll_adds_leader_tokens_to_the_board_tagged_and_caches_amounts(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    store, guard = _guard(tmp_path)
    guard.oracle = FakeOracle({A: quote(A)})
    rpc = FakeRpc({"addr1": [held(A, 1_000)]})
    asyncio.run(guard.poll_leader_holdings(rpc, [("ryantrost", "addr1")], CONFIG))
    board = guard.recommendations.candidates[f"solana:{A}"]
    assert board.sources == [SOURCE]
    assert guard.leader_holdings[A] == {"ryantrost": 1_000}
    # The same token later found by the momentum feed carries both sources.
    guard.recommendations.add(quote(A), guard.intelligence.score(quote(A)),
                              source="solana-momentum")
    assert board.sources == [SOURCE, "solana-momentum"]
    store.close()


async def _no_sleep(*_args, **_kwargs):
    return None


def test_a_token_only_the_leader_feed_found_is_never_bought_live():
    profile = StrategyProfile(entry_allowed_decisions=("BUY ZONE",))
    assert "shadow-only source leader-held" in profile.entry_block_reason(
        "BUY ZONE", 1, sources=["leader-held"]
    )
    assert profile.entry_block_reason(
        "BUY ZONE", 1, sources=["leader-held", "solana-momentum"]
    ) is None
    assert profile.entry_block_reason("BUY ZONE", 1, sources=None) is None


def test_env_can_release_the_leader_source(monkeypatch):
    monkeypatch.setenv("ENTRY_SHADOW_ONLY_SOURCES", "")
    profile = StrategyProfile.from_env()
    assert profile.entry_block_reason("BUY ZONE", 1, sources=["leader-held"]) is None
    monkeypatch.setenv("FEED_LEADER_HOLDINGS", "true")
    assert StrategyProfile.from_env().feed_leader_holdings is True


def test_auto_buyer_skips_a_leader_only_token(tmp_path):
    store, guard = guard_with(tmp_path, StrategyProfile())
    c = candidate("L", "BUY ZONE")
    c.sources = ["leader-held"]
    guard.settings = settings(tmp_path / "g.db", auto_buy_enabled=True,
                              auto_buy_discovery=True)
    asyncio.run(guard._maybe_auto_buy(c))
    assert "shadow-only source" in guard.entry_block_logged["L"]
    assert store.load_auto_buy_policy("L") is None
    store.close()


def test_snapshot_and_signal_log_carry_the_sources(tmp_path):
    store, guard = guard_with(tmp_path, StrategyProfile())
    c = candidate("L", "BUY ZONE")
    c.sources = ["leader-held"]
    guard.recommendations.candidates["L"] = c
    [row] = build_snapshot([c], pending_count=0, poll_seconds=15)["candidates"]
    assert row["sources"] == ["leader-held"]
    guard._record_buy_signals()
    got = store.connection.execute(
        "SELECT sources, live_blocked_reason FROM buy_signals"
    ).fetchone()
    assert got[0] == "leader-held" and "shadow-only source" in got[1]
    store.close()


def test_tracker_tags_signals_with_their_source(tmp_path):
    launch_db = tmp_path / "launch_guard.db"
    _make_launch_db(launch_db, [])
    SQLiteStore(str(launch_db)).close()
    db = sqlite3.connect(launch_db)
    for i, found_by in enumerate(("leader-held,solana-momentum", "solana-momentum")):
        db.execute(
            "INSERT INTO buy_signals(signaled_at, mint, symbol, chain, decision, "
            "sources) VALUES (?, ?, 'X', 'solana', 'BUY ZONE', ?)",
            (datetime.fromtimestamp(1000.0 + i, UTC).isoformat(), f"M{i}", found_by),
        )
    db.commit()
    db.close()
    store = OutcomeStore(tmp_path / "o.db")
    OutcomeTracker(store, FakeClient({}), TrackerConfig(), launch_db=launch_db,
                   ledger_db=None, clock=lambda: 1010.0).ingest()
    tags = {d.mint: [r for r in d.reasons if r.startswith("source: ")]
            for d in store.load()}
    assert tags == {"M0": ["source: leader-held,solana-momentum"],
                    "M1": ["source: solana-momentum"]}
    store.close()


def test_report_splits_signals_by_source():
    def signal(i, source, exit_price):
        obs = (Observation(i * 1000 + 5, 1.0, 9000, True),
               Observation(i * 1000 + 65, exit_price, 9000, True))
        return TrackedDecision("signal", f"m{i}", i * 1000.0, "BUY ZONE",
                               (f"source: {source}",), obs)
    decisions = [signal(i, "leader-held", 1.4) for i in range(3)]
    decisions += [signal(10 + i, "solana-momentum", 0.8) for i in range(3)]
    report = build_report(decisions, ExitRules(), CostModel(), train_fraction=0.5,
                          min_category_size=2)
    rows = report["signal_sources"]["signal:BUY ZONE"]
    assert [r["pattern"] for r in rows] == ["leader-held", "solana-momentum"]


def _sell(wallet, mint, amount):
    return WalletTrade(wallet=wallet, signature=f"sig-{amount}", slot=1, mint=mint,
                       side="SELL", token_delta=amount, native_sol_delta=0.0,
                       usdc_delta=50.0)


def _warning_guard(tmp_path):
    store, guard = _guard(tmp_path, copyfomo_leader_wallets=(("ryantrost", B),))

    class Oracle:
        async def quote(self, mint, *, chain="solana"):
            return None

    guard.oracle = Oracle()
    return store, guard


def _events(store):
    return store.connection.execute(
        "SELECT payload_json FROM events WHERE event_kind = 'LEADER_SELL'"
    ).fetchall()


def test_leader_selling_a_big_share_of_a_board_token_is_recorded(tmp_path):
    store, guard = _warning_guard(tmp_path)
    guard.recommendations.candidates[f"solana:{A}"] = candidate(A, "WATCH")
    guard.leader_holdings[A] = {"ryantrost": 1_000.0}
    asyncio.run(guard.handle_copyfomo_solana_trade(_sell(B, A, 100)))  # 10%
    assert _events(store) == []
    asyncio.run(guard.handle_copyfomo_solana_trade(_sell(B, A, 600)))  # 67% of 900
    [(payload,)] = _events(store)
    event = json.loads(payload)
    assert event["fraction_of_holding"] == pytest.approx(600 / 900)
    assert event["on_board"] is True and event["held_by_us"] is False
    assert guard.leader_holdings[A]["ryantrost"] == pytest.approx(300)
    store.close()


def test_leader_sells_of_tokens_we_do_not_follow_are_not_recorded(tmp_path):
    store, guard = _warning_guard(tmp_path)
    asyncio.run(guard.handle_copyfomo_solana_trade(_sell(B, A, 1_000)))
    assert _events(store) == []
    store.close()
