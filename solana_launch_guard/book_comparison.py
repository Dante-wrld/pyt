"""Side-by-side results of the paper books, judged on risk as well as profit.

Safety is part of performance, so every book is scored on the same terms:

- return per dollar risked: net realized P&L / total cost of the closed
  positions (swing-v1 can put $10 into one position, the others $5, so
  dollar totals alone would flatter whichever risks more);
- the worst single trade and the deepest drawdown of the realized equity
  curve, in dollars and as a share of the $30 starting capital;
- win rate, profit factor, and a 95% bootstrap interval on the per-trade
  return, so a lucky streak is not read as an edge.

A trade is one position from its opening buy to its final sale; partial
sales (principal, second stage) and averaging-down adds belong to it. The
books are read only; nothing is written.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .evaluation import bootstrap_mean_ci

MIN_TRADES_FOR_VERDICT = 30


@dataclass
class BookResult:
    name: str
    description: str
    starting_usd: float
    trades: int = 0
    wins: int = 0
    net_usd: float = 0.0
    risked_usd: float = 0.0
    worst_trade_usd: float = 0.0
    max_drawdown_usd: float = 0.0
    gross_profit_usd: float = 0.0
    gross_loss_usd: float = 0.0
    open_positions: int = 0
    open_cost_usd: float = 0.0
    open_unrealized_usd: float = 0.0
    return_ci95: tuple[float, float] | None = None
    missing: bool = False

    @property
    def return_per_dollar(self) -> float | None:
        return self.net_usd / self.risked_usd if self.risked_usd else None

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.trades if self.trades else None

    @property
    def profit_factor(self) -> float | None:
        if not self.gross_loss_usd:
            return None
        return self.gross_profit_usd / self.gross_loss_usd

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update(return_per_dollar=self.return_per_dollar,
                    win_rate=self.win_rate, profit_factor=self.profit_factor)
        return data


def _load(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def score_account(
    name: str, description: str, account: dict[str, Any] | None,
    *, keep: Callable[[dict[str, Any]], bool] | None = None,
) -> BookResult:
    """Score one agent account from a CapitalBook payload. `keep` selects a
    slice of it (applied to every fill and open position)."""
    if account is None:
        return BookResult(name, description, 30.0, missing=True)
    keep = keep or (lambda _row: True)
    result = BookResult(name, description,
                        float(account.get("starting_capital_usd") or 30.0))
    trades: dict[tuple[str, str], dict[str, Any]] = {}
    for index, fill in enumerate(account.get("completed_trades", [])):
        if not keep(fill):
            continue
        key = (str(fill.get("mint")), str(fill.get("opened_at", f"legacy-{index}")))
        trade = trades.setdefault(key, {"cost": 0.0, "pnl": 0.0, "closed": False,
                                        "closed_at": ""})
        trade["cost"] += float(fill.get("entry_value_usd") or 0.0)
        trade["pnl"] += float(fill.get("realized_pnl_usd") or 0.0)
        if fill.get("position_closed", True):
            trade["closed"] = True
            trade["closed_at"] = str(fill.get("closed_at") or "")
    closed = sorted((t for t in trades.values() if t["closed"]),
                    key=lambda t: t["closed_at"])
    equity = peak = 0.0
    returns: list[float] = []
    for trade in closed:
        pnl, cost = trade["pnl"], trade["cost"]
        result.trades += 1
        result.net_usd += pnl
        result.risked_usd += cost
        result.wins += pnl > 0
        if pnl > 0:
            result.gross_profit_usd += pnl
        else:
            result.gross_loss_usd -= pnl
        result.worst_trade_usd = min(result.worst_trade_usd, pnl)
        if cost > 0:
            returns.append(pnl / cost)
        equity += pnl
        peak = max(peak, equity)
        result.max_drawdown_usd = max(result.max_drawdown_usd, peak - equity)
    result.return_ci95 = bootstrap_mean_ci(returns)
    for position in (account.get("positions") or {}).values():
        if not keep(position):
            continue
        cost = float(position.get("allocated_usd") or 0.0)
        value = float(position.get("current_value_usd", cost) or 0.0)
        result.open_positions += 1
        result.open_cost_usd += cost
        result.open_unrealized_usd += value - cost
    return result


def wide_slices(account: dict[str, Any] | None) -> list[BookResult]:
    """wide-v1 overall, then split by whether the live gate would have taken
    each trade, then by signal type."""
    rows = [score_account(
        "wide-v1", "every BUY_READY signal kind, any token age; hunter exits",
        account)]
    if account is None:
        return rows
    rows.append(score_account(
        "wide:live-too", "trades the frozen live gate would also take", account,
        keep=lambda r: not r.get("live_blocked")))
    rows.append(score_account(
        "wide:frozen-out", "trades only this book takes (the freeze blocks them)",
        account, keep=lambda r: bool(r.get("live_blocked"))))
    kinds = sorted({str(r.get("decision")) for r in
                    list(account.get("completed_trades", []))
                    + list((account.get("positions") or {}).values())
                    if r.get("decision")})
    for kind in kinds:
        rows.append(score_account(
            f"wide:{kind}", f"wide-v1 {kind} trades only", account,
            keep=_decision_is(kind)))
    return rows


def _decision_is(kind: str) -> Callable[[dict[str, Any]], bool]:
    return lambda row: row.get("decision") == kind


def load_books(
    *, swing_book: Path, trend_directory: Path, hunter_book: Path | None,
    wide_book: Path | None = None,
) -> list[BookResult]:
    """Every paper book that exists, most comparable first."""
    books: list[BookResult] = []
    for arm, description in (
        ("baseline", "hunter entries, normal exits (trend experiment)"),
        ("trend", "EMA-trend-filtered entries, normal exits"),
        ("managed", "hunter entries + swing manager (normal stop, no adds)"),
    ):
        payload = _load(trend_directory / f"{arm}.json")
        account = payload.get("agents", {}).get("hunter-v1") if payload else None
        books.append(score_account(f"trend:{arm}", description, account))
    payload = _load(swing_book)
    books.append(score_account(
        "swing-v1", "own entries, 35% stop from avg cost, up to 2 adds, 24h",
        payload.get("agents", {}).get("swing-v1") if payload else None))
    if wide_book is not None:
        payload = _load(wide_book)
        books.extend(wide_slices(
            payload.get("agents", {}).get("wide-v1") if payload else None))
    if hunter_book is not None:
        payload = _load(hunter_book)
        books.append(score_account(
            "hunter-v1", "the paper hunter (model-approved entries; its own "
            "slippage estimates, so costs differ)",
            payload.get("agents", {}).get("hunter-v1") if payload else None))
    return books


def verdict(books: Sequence[BookResult]) -> str:
    # Slices of one book (wide:...) are shown for detail, not ranked against it.
    ready = [b for b in books if not b.missing and not b.name.startswith("wide:")
             and b.trades >= MIN_TRADES_FOR_VERDICT]
    if len(ready) < 2:
        return (f"Too early: fewer than two books have {MIN_TRADES_FOR_VERDICT} "
                "closed trades. Differences so far are noise.")
    ranked = sorted(ready, key=lambda b: b.return_per_dollar or 0.0, reverse=True)
    best, second = ranked[0], ranked[1]
    line = (f"Best return per dollar risked: {best.name} "
            f"({(best.return_per_dollar or 0) * 100:+.1f}% vs "
            f"{second.name} {(second.return_per_dollar or 0) * 100:+.1f}%).")
    a, b = best.return_ci95, second.return_ci95
    if a and b and a[0] <= b[1]:
        line += " Their 95% intervals overlap: not a proven difference yet."
    safer = min(ready, key=lambda b: b.max_drawdown_usd)
    if safer is not best:
        line += (f" {safer.name} had the smaller drawdown "
                 f"(${safer.max_drawdown_usd:.2f} vs ${best.max_drawdown_usd:.2f}); "
                 "if the returns are close, prefer the safer one.")
    return line


def render(books: Sequence[BookResult]) -> str:
    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value * 100:+.1f}%"

    lines = ["Paper books (realized, after the cost each book charges)",
             f"{'book':<15} {'trades':>6} {'win':>5} {'net $':>8} {'per $':>7} "
             f"{'worst $':>8} {'max DD $':>9} {'DD/start':>8} {'PF':>5}  95% CI/trade"]
    for b in books:
        if b.missing:
            lines.append(f"{b.name:<15} (no book yet)")
            continue
        ci = (f"{b.return_ci95[0] * 100:+.1f}% to {b.return_ci95[1] * 100:+.1f}%"
              if b.return_ci95 else "n/a")
        win = "n/a" if b.win_rate is None else f"{b.win_rate * 100:.0f}%"
        dd_share = b.max_drawdown_usd / b.starting_usd * 100
        pf = "n/a" if b.profit_factor is None else f"{b.profit_factor:.2f}"
        lines.append(
            f"{b.name:<15} {b.trades:>6} {win:>5} {b.net_usd:>+8.2f} "
            f"{pct(b.return_per_dollar):>7} {b.worst_trade_usd:>+8.2f} "
            f"{b.max_drawdown_usd:>9.2f} {dd_share:>7.1f}% "
            f"{pf:>5}  {ci}")
        if b.open_positions:
            lines.append(f"{'':<15} open {b.open_positions}: "
                         f"cost ${b.open_cost_usd:.2f}, "
                         f"unrealized {b.open_unrealized_usd:+.2f}")
    lines.append("")
    for b in books:
        lines.append(f"  {b.name:<15} {b.description}")
    lines += ["", "per $ = net P&L / total cost of closed positions (swing-v1 can risk "
              "$10 in one position, the others $5).",
              "DD/start = deepest realized drawdown as a share of the $30 book.",
              "", verdict(books)]
    return "\n".join(lines)
