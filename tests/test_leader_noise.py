"""Leaders' raw trade counts are inflated by things nobody could copy:
pump.fun PUMP rewards (tokens in, no payment) and tokenized stocks (TSLAx...).
Reported 2026-09-26: Rowdy's 168 trades were 153 PUMP receipts + 15 real."""
import sqlite3
from datetime import UTC, datetime

import pytest
from solana_launch_guard.copyfomo_report import (
    WalletTradeRow,
    attribute_leaders,
    build_copyfomo_report,
    clean_leader_trades,
    leader_profiles,
    looks_like_tokenized_stock,
    per_token,
    post_buy_premium,
    render_copyfomo_report,
)
from solana_launch_guard.core import SQLiteStore
from solana_launch_guard.eval_cli import _drop_uncopyable
from solana_launch_guard.evaluation import TrackedDecision

T0 = datetime(2026, 9, 26, tzinfo=UTC).timestamp()


def row(mint, side="BUY", *, sol=-1.0, usdc=None, tokens=100.0, symbol=None,
        price=None, t=0.0):
    return WalletTradeRow(T0 + t, f"{mint}-{side}-{t}", mint, symbol or mint, side,
                          tokens, sol, usdc, price)


def test_stock_symbols_are_recognized_offline():
    for symbol in ("TSLAx", "NVDAx", "SPCXx", "GLDx", "wNVDAx"):
        assert looks_like_tokenized_stock(symbol, "M" * 44)
    assert looks_like_tokenized_stock("weird", "Xs" + "1" * 42)  # xStocks mint
    for symbol in ("wifout", "FEELSGOOD", "STONK", "C𝕏", "x", "PUMP"):
        assert not looks_like_tokenized_stock(symbol, "M" * 44)


def test_rewards_and_stocks_are_set_aside_and_counted():
    trades = [row("PUMP", sol=0.0, usdc=0.0) for _ in range(3)]
    trades += [row("TSLA", symbol="TSLAx"),
               row("TSLA", "SELL", sol=1.2, symbol="TSLAx")]
    trades += [row("STONK")]
    kept, noise = clean_leader_trades(trades)
    assert [t.mint for t in kept] == ["STONK"]
    assert noise == {"stock_trades": 2, "unpaid_receipts": 3}


def test_leader_rows_count_only_real_positions():
    rows = attribute_leaders([], {"rowdy": [
        row("PUMP", sol=0.0), row("PUMP", sol=None, t=1),
        row("NVDA", symbol="NVDAx", t=2),
        row("SDOG", t=3), row("SDOG", "SELL", sol=0.5, t=4),
    ]})
    assert [(r["leader"], r["symbol"]) for r in rows] == [("rowdy", "SDOG")]


def test_post_buy_premium_compares_the_seen_price_with_their_fill():
    # Paid 1 SOL for 100 tokens (0.01 each); price was 0.0115 when seen.
    assert post_buy_premium(row("A", price=0.0115)) == pytest.approx(0.15)
    assert post_buy_premium(row("A", sol=None, usdc=-150, price=0.0115)) is None
    assert post_buy_premium(row("A", sol=0.0, price=0.0115)) is None  # a reward
    assert post_buy_premium(row("A", price=1.5)) is None  # an old USD-priced row


def test_profiles_and_render():
    trades = {"pointfarmcap": [row("A", price=0.011), row("B", price=0.013, t=1),
                               row("C", price=0.012, t=2),
                               row("PUMP", sol=0.0, t=3), row("T", symbol="GLDx")]}
    profile = leader_profiles(trades)["pointfarmcap"]
    assert (profile["raw_trades"], profile["paid_buys"]) == (5, 3)
    assert (profile["unpaid_receipts"], profile["stock_trades"]) == (1, 1)
    assert profile["premium_median"] == pytest.approx(0.2)
    report = build_copyfomo_report(per_token([row("A")]), [], leader_profiles(trades))
    text = render_copyfomo_report(report, "C" * 44)
    assert "paid buys 3" in text and "dropped 1 unpaid, 1 stock" in text
    assert "+20.0% vs their fill" in text


def test_report_drops_wallet_decisions_tracked_before_the_filter(tmp_path):
    db_path = tmp_path / "launch_guard.db"
    SQLiteStore(str(db_path)).close()
    db = sqlite3.connect(db_path)
    for i, (mint, symbol, sol) in enumerate([
        ("REAL", "real", -0.5), ("PUMPM", "PUMP", None), ("TSLAM", "TSLAx", -0.5),
    ], start=1):
        db.execute(
            "INSERT INTO wallet_trades(id, seen_at, wallet, signature, slot, mint, "
            "side, token_delta, native_sol_delta, symbol) "
            "VALUES (?, '2026-09-26T00:00:00+00:00', 'W', ?, ?, ?, 'BUY', 1, ?, ?)",
            (i, f"s{i}", i, mint, sol, symbol),
        )
    db.commit()
    db.close()
    decisions = [
        TrackedDecision("wallet", m, T0, "leader:rowdy", (), (), source_id=i)
        for i, m in enumerate(["REAL", "PUMPM", "TSLAM"], start=1)
    ] + [TrackedDecision("signal", "SIG", T0, "BUY ZONE", (), (), source_id=2)]
    kept = _drop_uncopyable(decisions, str(db_path))
    assert [d.mint for d in kept] == ["REAL", "SIG"]
