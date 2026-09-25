"""What CopyFomo actually made, from the trades its wallet recorded.

CopyFomo trades real money from its own wallet, and the monitor stores every
trade with ``native_sol_delta``: the SOL that actually left or entered the
wallet in that transaction, after fees and slippage. Summing it per token
gives CopyFomo's true realized result with no simulation.

Every trade already carries its exact mint from on-chain data, so nothing
here matches tokens by name.

A token's result is only counted when every leg of it can be priced from
SOL flow. Legs that cannot be are set aside and reported, never guessed:

- A transaction that moved more than one token (a token-to-token swap):
  the one SOL delta cannot be split between them.
- A buy with no real SOL outflow or a sell with no real SOL inflow: it was
  routed through wrapped SOL or a stablecoin, or it was an airdrop.
- Sells of tokens bought before monitoring began.

Only fully closed positions (at least 99% of bought tokens sold) count as
realized; open ones are listed with their SOL cost.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .evaluation import bootstrap_mean_ci

FEE_ONLY_SOL = 0.002  # a real $5+ swap moves far more SOL than this
CLOSED_FRACTION = 0.99


@dataclass(frozen=True, slots=True)
class WalletTradeRow:
    seen_at: float
    signature: str
    mint: str
    symbol: str
    side: str
    token_delta: float
    native_sol_delta: float | None


@dataclass(slots=True)
class TokenResult:
    mint: str
    symbol: str
    first_buy_at: float | None = None
    last_trade_at: float = 0.0
    tokens_bought: float = 0.0
    tokens_sold: float = 0.0
    sol_spent: float = 0.0
    sol_received: float = 0.0
    buys: int = 0
    sells: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.problems:
            return "UNPRICED"
        if self.tokens_bought <= 0:
            return "UNPRICED"
        if self.tokens_sold >= self.tokens_bought * CLOSED_FRACTION:
            return "CLOSED"
        return "OPEN"

    @property
    def realized_sol(self) -> float | None:
        if self.status != "CLOSED":
            return None
        return self.sol_received - self.sol_spent

    def as_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status
        data["realized_sol"] = self.realized_sol
        return data


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def load_trades(db_path: str | Path, wallet: str) -> list[WalletTradeRow]:
    path = Path(db_path)
    if not path.exists():
        return []
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT seen_at, signature, mint, symbol, side, token_delta, "
            "native_sol_delta FROM wallet_trades WHERE wallet = ? ORDER BY slot, id",
            (wallet,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()
    return [
        WalletTradeRow(
            _epoch(seen_at), signature, mint, symbol or mint[:6], side,
            float(token_delta), None if sol is None else float(sol),
        )
        for seen_at, signature, mint, symbol, side, token_delta, sol in rows
    ]


def per_token(trades: Sequence[WalletTradeRow]) -> list[TokenResult]:
    mints_per_signature: dict[str, set[str]] = defaultdict(set)
    for trade in trades:
        mints_per_signature[trade.signature].add(trade.mint)

    results: dict[str, TokenResult] = {}
    for trade in trades:
        token = results.setdefault(trade.mint, TokenResult(trade.mint, trade.symbol))
        token.last_trade_at = max(token.last_trade_at, trade.seen_at)
        sol = trade.native_sol_delta
        if len(mints_per_signature[trade.signature]) > 1:
            token.problems.append("token-to-token swap")
            continue
        if trade.side == "BUY":
            if sol is None or sol > -FEE_ONLY_SOL:
                token.problems.append(
                    "buy without SOL outflow (WSOL/USDC route or airdrop)"
                )
                continue
            token.buys += 1
            token.tokens_bought += trade.token_delta
            token.sol_spent += -sol
            if token.first_buy_at is None:
                token.first_buy_at = trade.seen_at
        else:
            if token.tokens_bought <= 0:
                token.problems.append("sold before any recorded buy")
                continue
            if sol is None or sol < FEE_ONLY_SOL:
                token.problems.append("sell without SOL inflow (WSOL/USDC route)")
                continue
            token.sells += 1
            token.tokens_sold += trade.token_delta
            token.sol_received += sol
    for token in results.values():
        token.problems = sorted(set(token.problems))
    return sorted(results.values(), key=lambda t: t.last_trade_at)


def _week(epoch: float) -> str:
    year, week, _ = datetime.fromtimestamp(epoch).isocalendar()
    return f"{year}-W{week:02d}"


def build_copyfomo_report(tokens: Sequence[TokenResult]) -> dict:
    closed = [t for t in tokens if t.status == "CLOSED"]
    pnls = [t.realized_sol or 0.0 for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    weeks: dict[str, list[float]] = defaultdict(list)
    for token in closed:
        weeks[_week(token.last_trade_at)].append(token.realized_sol or 0.0)
    return {
        "closed_positions": len(closed),
        "open_positions": sum(t.status == "OPEN" for t in tokens),
        "unpriced_tokens": sum(t.status == "UNPRICED" for t in tokens),
        "realized_sol": sum(pnls),
        "win_rate": len(wins) / len(pnls) if pnls else None,
        "avg_win_sol": sum(wins) / len(wins) if wins else None,
        "avg_loss_sol": sum(losses) / len(losses) if losses else None,
        "per_position_ci95_sol": bootstrap_mean_ci(pnls),
        "weeks": {
            week: {
                "positions": len(values),
                "realized_sol": sum(values),
                "win_rate": sum(v > 0 for v in values) / len(values),
            }
            for week, values in sorted(weeks.items())
        },
        "tokens": [t.as_dict() for t in tokens],
    }


def render_copyfomo_report(report: dict, wallet: str) -> str:
    lines = [f"CopyFomo wallet {wallet[:6]}...{wallet[-4:]} (SOL, after all fees)"]
    if not report["tokens"]:
        lines.append(
            "No trades recorded yet. Set FEED_COPYFOMO_WALLETS=true and "
            "COPYFOMO_SOLANA_WALLET, then let the bot run."
        )
        return "\n".join(lines)

    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value * 100:.0f}%"

    def sol(value: float | None) -> str:
        return "n/a" if value is None else f"{value:+.4f}"

    ci = report["per_position_ci95_sol"]
    lines.append(
        f"Closed {report['closed_positions']} | open {report['open_positions']} | "
        f"unpriced {report['unpriced_tokens']}"
    )
    lines.append(
        f"Realized {sol(report['realized_sol'])} SOL | win rate "
        f"{pct(report['win_rate'])} | avg win {sol(report['avg_win_sol'])} | "
        f"avg loss {sol(report['avg_loss_sol'])}"
        + (f" | per position 95% CI {ci[0]:+.4f} to {ci[1]:+.4f}" if ci else "")
    )
    if report["closed_positions"] < 30:
        lines.append("  -> fewer than 30 closed positions: too early for a verdict")
    elif ci and ci[0] > 0:
        lines.append("  -> profitable after fees, and the 95% CI is above zero")
    elif ci and ci[1] < 0:
        lines.append("  -> losing after fees, and the 95% CI is below zero")
    else:
        lines.append("  -> indistinguishable from zero so far")

    if report["weeks"]:
        lines.append("\nBy week (positions closed that week):")
        for week, data in report["weeks"].items():
            lines.append(
                f"  {week}  n={data['positions']:<3} {sol(data['realized_sol'])} SOL"
                f"  win {pct(data['win_rate'])}"
            )

    lines.append("\nPositions (latest last):")
    for token in report["tokens"][-25:]:
        detail = (
            f"{sol(token['realized_sol'])} SOL" if token["status"] == "CLOSED"
            else f"cost {token['sol_spent']:.4f} SOL, "
            f"{token['tokens_sold'] / token['tokens_bought'] * 100:.0f}% sold"
            if token["status"] == "OPEN"
            else "; ".join(token["problems"])
        )
        lines.append(f"  {token['symbol'][:12]:<12} {token['status']:<8} {detail}")
    return "\n".join(lines)
