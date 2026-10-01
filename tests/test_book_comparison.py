"""`launch-guard-eval books`: the paper books side by side, scored on risk
as well as profit (2026-09-27)."""
import pytest
from solana_launch_guard.book_comparison import (
    MIN_TRADES_FOR_VERDICT,
    load_books,
    render,
    score_account,
    verdict,
)
from solana_launch_guard.eval_cli import main as eval_main
from solana_launch_guard.swing_strategy import SWING_AGENT_ID, SwingCapitalBook
from solana_launch_guard.trend_shadow import ResearchCapitalBook
from solana_launch_guard.wide_fresh_shadow import (
    WIDE_FRESH_AGENT_ID,
    WideFreshCapitalBook,
)


def _trade(book, agent, mint, exit_price, *, add_at=None, partial=False):
    book.reserve_shadow_buy(agent_id=agent, mint=mint, symbol=mint[:3],
                            amount_usd=5, entry_price=1.0, price_currency="USD")
    if add_at is not None:
        book.mark_shadow_position(agent, mint, add_at)
        book.add_to_shadow_position(agent, mint, amount_usd=2.5, price=add_at)
    if partial:
        book.mark_shadow_position(agent, mint, 2.0)
        book.close_shadow_position(agent, mint, fraction=0.5,
                                   stage="PRINCIPAL_RECOVERY")
    book.mark_shadow_position(agent, mint, exit_price)
    book.close_shadow_position(agent, mint)


def _account(book, agent):
    return book.load()["agents"][agent]


def test_scores_per_position_including_adds_and_partials(tmp_path):
    book = SwingCapitalBook(tmp_path / "swing.json")
    book.initialize(30)
    _trade(book, SWING_AGENT_ID, "A" * 44, 1.2)                    # +$1.00
    _trade(book, SWING_AGENT_ID, "B" * 44, 0.6, add_at=0.8)       # adds, loses
    _trade(book, SWING_AGENT_ID, "C" * 44, 1.5, partial=True)     # one trade
    result = score_account("swing-v1", "", _account(book, SWING_AGENT_ID))
    assert result.trades == 3 and result.wins == 2
    # B: $7.50 in, 5 + 3.125 tokens at 0.6 = $4.875 back.
    assert result.worst_trade_usd == pytest.approx(4.875 - 7.5)
    assert result.risked_usd == pytest.approx(5 + 7.5 + 5)
    assert result.return_per_dollar == pytest.approx(result.net_usd / 17.5)
    assert result.max_drawdown_usd == pytest.approx(7.5 - 4.875)


def test_drawdown_follows_the_realized_equity_curve():
    fills = [{"mint": m, "opened_at": m, "closed_at": f"2026-09-27T0{i}:00:00",
              "entry_value_usd": 5, "realized_pnl_usd": p, "position_closed": True}
             for i, (m, p) in enumerate([("a", 2), ("b", -1), ("c", -1.5), ("d", 1)])]
    result = score_account("x", "", {"starting_capital_usd": 30,
                                     "completed_trades": fills, "positions": {}})
    assert result.max_drawdown_usd == pytest.approx(2.5)
    assert result.profit_factor == pytest.approx(3 / 2.5)


def _many(result, n, pnl, cost=5.0):
    fills = [{"mint": f"m{i}", "opened_at": str(i), "closed_at": f"{i:04d}",
              "entry_value_usd": cost, "realized_pnl_usd": pnl(i),
              "position_closed": True} for i in range(n)]
    return score_account(result, "", {"starting_capital_usd": 30,
                                      "completed_trades": fills, "positions": {}})


def test_distinct_tokens_counts_the_mints_not_the_trades():
    fills = [{"mint": "A" * 44, "opened_at": "1", "closed_at": "0001",
              "entry_value_usd": 5, "realized_pnl_usd": -0.1, "position_closed": True},
             {"mint": "A" * 44, "opened_at": "2", "closed_at": "0002",
              "entry_value_usd": 5, "realized_pnl_usd": -0.1, "position_closed": True},
             {"mint": "B" * 44, "opened_at": "3", "closed_at": "0003",
              "entry_value_usd": 5, "realized_pnl_usd": 1.0, "position_closed": True}]
    result = score_account("x", "", {"starting_capital_usd": 30,
                                     "completed_trades": fills, "positions": {}})
    assert result.trades == 3 and result.distinct_tokens == 2


def test_verdict_waits_for_enough_trades():
    assert "Too early" in verdict([_many("a", 5, lambda i: 1),
                                   _many("b", 5, lambda i: -1)])


def test_verdict_compares_per_dollar_and_names_the_safer_book():
    n = MIN_TRADES_FOR_VERDICT
    risky = _many("swing-v1", n, lambda i: 3 if i % 2 else -3.4, cost=10)
    safe = _many("trend:managed", n, lambda i: 0.2 if i % 2 else -0.15)
    text = verdict([risky, safe])
    assert text.startswith("Best return per dollar risked: trend:managed")
    risky_up = _many("swing-v1", n, lambda i: 4 if i % 2 else -3, cost=10)
    text = verdict([risky_up, safe])
    assert "swing-v1" in text.split(":")[1]
    assert "trend:managed had the smaller drawdown" in text


def test_command_reads_every_book_and_tolerates_missing_ones(tmp_path, capsys):
    swing = SwingCapitalBook(tmp_path / "swing.json")
    swing.initialize(30)
    _trade(swing, SWING_AGENT_ID, "A" * 44, 1.2)
    managed = ResearchCapitalBook(tmp_path / "trend" / "managed.json")
    managed.initialize(30)
    _trade(managed, "hunter-v1", "B" * 44, 0.9)
    eval_main(["books", "--swing-book", str(tmp_path / "swing.json"),
               "--trend-dir", str(tmp_path / "trend"),
               "--hunter-book", str(tmp_path / "missing.json")])
    out = capsys.readouterr().out
    assert "swing-v1" in out and "trend:managed" in out
    assert "trend:baseline  (no book yet)" in out
    assert "Too early" in out
    books = load_books(swing_book=tmp_path / "swing.json",
                       trend_directory=tmp_path / "trend", hunter_book=None)
    assert "per $" in render(books)


def test_wide_fresh_book_appears_alongside_wide_v1(tmp_path, capsys):
    swing = SwingCapitalBook(tmp_path / "swing.json")
    swing.initialize(30)
    wide_fresh = WideFreshCapitalBook(tmp_path / "wide_fresh.json")
    wide_fresh.initialize(30)
    _trade(wide_fresh, WIDE_FRESH_AGENT_ID, "A" * 44, 1.2)
    eval_main(["books", "--swing-book", str(tmp_path / "swing.json"),
               "--trend-dir", str(tmp_path / "trend"),
               "--hunter-book", str(tmp_path / "missing.json"),
               "--wide-book", str(tmp_path / "missing_wide.json"),
               "--wide-fresh-book", str(tmp_path / "wide_fresh.json")])
    out = capsys.readouterr().out
    assert "wide-fresh-v1" in out and "wide-fresh:frozen-out" in out
    books = load_books(swing_book=tmp_path / "swing.json",
                       trend_directory=tmp_path / "trend", hunter_book=None,
                       wide_fresh_book=tmp_path / "wide_fresh.json")
    names = [b.name for b in books]
    assert "wide-fresh-v1" in names and "wide:frozen-out" not in names
