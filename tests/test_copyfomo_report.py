import asyncio
from datetime import UTC, datetime

import pytest
from solana_launch_guard.app import LaunchGuard
from solana_launch_guard.config import _dotenv_value, _load_dotenv
from solana_launch_guard.copyfomo_report import (
    WalletTradeRow,
    build_copyfomo_report,
    load_trades,
    per_token,
    render_copyfomo_report,
)
from solana_launch_guard.core import SQLiteStore
from solana_launch_guard.eval_cli import main
from solana_launch_guard.market import MarketQuote
from solana_launch_guard.wallet import WalletTrade

from test_core import settings

WALLET = "CopyFomoWallet1111111111111111111111111111"
T0 = datetime(2026, 9, 21, tzinfo=UTC).timestamp()


def trade(sig, mint, side, tokens, sol, t=0.0, symbol=None):
    return WalletTradeRow(T0 + t, sig, mint, symbol or mint, side, tokens, sol)


def test_closed_position_realizes_exact_sol_flow():
    [token] = per_token([
        trade("s1", "WIN", "BUY", 1000, -0.10),
        trade("s2", "WIN", "SELL", 600, 0.09, t=60),
        trade("s3", "WIN", "SELL", 400, 0.07, t=120),
    ])
    assert token.status == "CLOSED"
    assert token.realized_sol == pytest.approx(0.06)


def test_partly_sold_position_is_open_and_not_realized():
    [token] = per_token([
        trade("s1", "HOLD", "BUY", 1000, -0.10),
        trade("s2", "HOLD", "SELL", 500, 0.08, t=60),
    ])
    assert token.status == "OPEN"
    assert token.realized_sol is None


def test_unpriceable_legs_are_set_aside_not_guessed():
    tokens = {t.mint: t for t in per_token([
        trade("sw", "A", "SELL", 10, -0.001),       # token-to-token swap:
        trade("sw", "B", "BUY", 50, -0.001),        # one SOL delta, two mints
        trade("air", "SPAM", "BUY", 1e9, -0.000005),  # airdrop / WSOL route
        trade("old", "PRE", "SELL", 10, 0.2),       # bought before monitoring
    ])}
    assert {t.status for t in tokens.values()} == {"UNPRICED"}
    assert "token-to-token swap" in tokens["A"].problems
    assert "sold before any recorded buy" in tokens["PRE"].problems
    report = build_copyfomo_report(list(tokens.values()))
    assert report["realized_sol"] == 0 and report["unpriced_tokens"] == 4


def test_report_totals_weeks_and_verdict():
    trades = []
    for i, (spent, got) in enumerate([(0.1, 0.15), (0.1, 0.02), (0.1, 0.12)]):
        trades += [
            trade(f"b{i}", f"M{i}", "BUY", 100, -spent, t=i * 86_400 * 4),
            trade(f"s{i}", f"M{i}", "SELL", 100, got, t=i * 86_400 * 4 + 60),
        ]
    report = build_copyfomo_report(per_token(trades))
    assert report["closed_positions"] == 3
    assert report["realized_sol"] == pytest.approx(0.09 - 0.1)
    assert report["win_rate"] == pytest.approx(2 / 3)
    assert list(report["weeks"]) == ["2026-W39", "2026-W40"]  # days 0,4 | 8
    assert "too early for a verdict" in render_copyfomo_report(report, WALLET)


def _guard_with_quote(tmp_path, price_sol, price_usd):
    database = tmp_path / "cf.db"
    store = SQLiteStore(str(database))
    guard = LaunchGuard(settings(database), store)

    class FakeOracle:
        async def quote(self, mint, *, chain="solana"):
            return MarketQuote(
                mint=mint, symbol="CPY", price_sol=price_sol, price_usd=price_usd,
                liquidity_usd=1, market_cap_usd=1, pair_address="P",
                pair_created_at_ms=1, buys_m5=0, sells_m5=0, volume_m5_usd=0,
                price_change_m5_pct=0,
            )

    guard.oracle = FakeOracle()
    return database, store, guard


def test_monitor_stores_the_sol_price_in_the_sol_column(tmp_path):
    database, store, guard = _guard_with_quote(tmp_path, 0.000002, 0.0003)
    asyncio.run(guard.handle_copyfomo_solana_trade(WalletTrade(
        wallet=WALLET, signature="sig", slot=1, mint="Mint1",
        side="BUY", token_delta=1000, native_sol_delta=-0.05,
    )))
    row = store.connection.execute(
        "SELECT observed_price_sol FROM wallet_trades"
    ).fetchone()
    assert row[0] == pytest.approx(0.000002)
    store.close()


def test_end_to_end_from_recorded_trades_to_cli(tmp_path, capsys):
    database, store, guard = _guard_with_quote(tmp_path, 0.000002, 0.0003)
    for sig, side, sol in [("b", "BUY", -0.05), ("s", "SELL", 0.08)]:
        asyncio.run(guard.handle_copyfomo_solana_trade(WalletTrade(
            wallet=WALLET, signature=sig, slot=1 if side == "BUY" else 2,
            mint="Mint1", side=side, token_delta=1000, native_sol_delta=sol,
        )))
    store.close()
    [token] = per_token(load_trades(database, WALLET))
    assert token.realized_sol == pytest.approx(0.03)
    main(["copyfomo", "--db", str(database), "--wallet", WALLET])
    out = capsys.readouterr().out
    assert "Realized +0.0300 SOL" in out and "CLOSED" in out


def test_cli_requires_a_wallet(monkeypatch):
    monkeypatch.delenv("COPYFOMO_SOLANA_WALLET", raising=False)
    with pytest.raises(SystemExit, match="COPYFOMO_SOLANA_WALLET"):
        main(["copyfomo", "--wallet", ""])


@pytest.mark.parametrize(("raw", "expected"), [
    ("true", "true"),
    ("true   # read-only monitor", "true"),
    ("   # just a comment", ""),
    ('"pa#ss word"', "pa#ss word"),
    ("'x # y'", "x # y"),
    ("https://rpc.example/#frag", "https://rpc.example/#frag"),
])
def test_dotenv_values_strip_inline_comments_like_dotenv(raw, expected):
    assert _dotenv_value(raw) == expected


def test_commented_env_block_loads_cleanly(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "FEED_COPYFOMO_WALLETS=true   # read-only\n"
        "COPYFOMO_SOLANA_WALLET=      # wallet address\n"
    )
    monkeypatch.delenv("FEED_COPYFOMO_WALLETS", raising=False)
    monkeypatch.delenv("COPYFOMO_SOLANA_WALLET", raising=False)
    _load_dotenv(str(env))
    import os
    assert os.environ["FEED_COPYFOMO_WALLETS"] == "true"
    assert os.environ["COPYFOMO_SOLANA_WALLET"] == ""
    monkeypatch.delenv("FEED_COPYFOMO_WALLETS")
    monkeypatch.delenv("COPYFOMO_SOLANA_WALLET")
