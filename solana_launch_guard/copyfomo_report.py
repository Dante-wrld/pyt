"""What CopyFomo actually made, from the trades its wallet recorded.

CopyFomo trades real money from its own wallet. For every trade the monitor
stores the token change plus what the wallet paid or received in the same
transaction: ``usdc_delta`` (CopyFomo's normal route, it trades in USDC) and
``native_sol_delta``. Those are on-chain amounts after every fee and all
slippage, so summing them per position gives CopyFomo's true realized
result with no simulation.

Every trade carries its exact mint from on-chain data, and nothing here
matches tokens by name. Positions are attributed to a leader by watching
the leaders' own wallets (COPYFOMO_LEADER_WALLETS): the leader whose buy of
the same mint came just before CopyFomo's is the one it copied.

Each leg is priced in USDC when the wallet's USDC balance moved by a real
amount in the right direction, otherwise in SOL, otherwise not at all. Legs
that cannot be priced are set aside and reported, never guessed:

- A transaction that moved more than one token (a token-to-token swap):
  the one payment cannot be split between them.
- A buy with no real outflow or a sell with no real inflow in either
  currency (an airdrop, or a route this parser does not see).
- A position whose legs were paid in different currencies.
- Sells of tokens bought before monitoring began.

A position opens with a buy and closes once at least 99% of what was bought
has been sold; a later buy of the same mint opens a new position. Only
closed positions count as realized.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .evaluation import bootstrap_mean_ci

FEE_ONLY_SOL = 0.002  # a real $2+ swap moves far more SOL than this
FEE_ONLY_USDC = 0.05  # and far more than 5 cents of USDC
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
    usdc_delta: float | None = None


@dataclass(slots=True)
class TokenResult:
    """One position: from its opening buy until it is fully sold."""

    mint: str
    symbol: str
    first_buy_at: float | None = None
    opening_signature: str | None = None
    last_trade_at: float = 0.0
    tokens_bought: float = 0.0
    tokens_sold: float = 0.0
    currency: str | None = None
    spent: float = 0.0
    received: float = 0.0
    buys: int = 0
    sells: int = 0
    leader: str | None = None
    leader_match: str | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.problems or self.tokens_bought <= 0:
            return "UNPRICED"
        if self.tokens_sold >= self.tokens_bought * CLOSED_FRACTION:
            return "CLOSED"
        return "OPEN"

    @property
    def realized(self) -> float | None:
        if self.status != "CLOSED":
            return None
        return self.received - self.spent

    def as_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status
        data["realized"] = self.realized
        return data


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def load_trades(db_path: str | Path, wallet: str) -> list[WalletTradeRow]:
    path = Path(db_path)
    if not path.exists():
        return []
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(wallet_trades)")
        }
        usdc = "usdc_delta" if "usdc_delta" in columns else "NULL"
        rows = connection.execute(
            "SELECT seen_at, signature, mint, symbol, side, token_delta, "
            f"native_sol_delta, {usdc} FROM wallet_trades WHERE wallet = ? "
            "ORDER BY slot, id",
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
            None if usdc_amount is None else float(usdc_amount),
        )
        for (seen_at, signature, mint, symbol, side, token_delta, sol,
             usdc_amount) in rows
    ]


def _payment(trade: WalletTradeRow) -> tuple[str, float] | None:
    """(currency, amount paid or received) for one leg, or None if unpriced."""
    sign = -1.0 if trade.side == "BUY" else 1.0
    usdc = trade.usdc_delta
    if usdc is not None and sign * usdc >= FEE_ONLY_USDC:
        return "USDC", abs(usdc)
    sol = trade.native_sol_delta
    if sol is not None and sign * sol >= FEE_ONLY_SOL:
        return "SOL", abs(sol)
    return None


def per_token(trades: Sequence[WalletTradeRow]) -> list[TokenResult]:
    """Group legs into positions per mint (see the module docstring)."""
    mints_per_signature: dict[str, set[str]] = defaultdict(set)
    for trade in trades:
        mints_per_signature[trade.signature].add(trade.mint)

    positions: list[TokenResult] = []
    current: dict[str, TokenResult] = {}
    for trade in trades:
        token = current.get(trade.mint)
        if token is None or token.status == "CLOSED":
            if trade.side != "BUY" and token is not None:
                # A sell after the position closed: dust, or a position we
                # never saw open. It belongs to no priced position.
                token.problems.append("sell after the position had closed")
                continue
            token = TokenResult(trade.mint, trade.symbol)
            current[trade.mint] = token
            positions.append(token)
        token.last_trade_at = max(token.last_trade_at, trade.seen_at)
        if len(mints_per_signature[trade.signature]) > 1:
            token.problems.append("token-to-token swap")
            continue
        if trade.side != "BUY" and token.tokens_bought <= 0:
            token.problems.append("sold before any recorded buy")
            continue
        payment = _payment(trade)
        if payment is None:
            token.problems.append(
                "buy without a real USDC or SOL outflow (airdrop or unseen route)"
                if trade.side == "BUY"
                else "sell without a real USDC or SOL inflow"
            )
            continue
        currency, amount = payment
        if token.currency is None:
            token.currency = currency
        elif token.currency != currency:
            token.problems.append("mixed SOL and USDC legs")
            continue
        if trade.side == "BUY":
            token.buys += 1
            token.tokens_bought += trade.token_delta
            token.spent += amount
            if token.first_buy_at is None:
                token.first_buy_at = trade.seen_at
                token.opening_signature = trade.signature
        else:
            token.sells += 1
            token.tokens_sold += trade.token_delta
            token.received += amount
    for token in positions:
        token.problems = sorted(set(token.problems))
    return sorted(positions, key=lambda t: t.last_trade_at)


# A copy lands shortly after the leader's buy. The window is generous because
# timestamps are when the monitor saw each transaction, not block time.
COPY_WINDOW_SECONDS = 600.0
COPY_EARLY_TOLERANCE_SECONDS = 30.0


def attribute_leaders(
    positions: Sequence[TokenResult],
    leader_trades: dict[str, Sequence[WalletTradeRow]],
    *,
    window_seconds: float = COPY_WINDOW_SECONDS,
) -> list[dict]:
    """Link each CopyFomo position to the leader whose buy of the SAME MINT
    came just before it, and list leader buys CopyFomo never copied.

    ``leader_trades`` maps a leader's name to their wallet's trades. Matching
    uses the mint and timing only. A position two different leaders could
    explain is marked ambiguous and left unattributed rather than guessed.
    Returns one row per leader position (bought, then fully sold or still
    open) with ``copied`` True/False, so leaders can be judged on the trades
    CopyFomo skipped as well as the ones it took.
    """
    leader_buys: list[tuple[float, str, str]] = []  # (time, mint, leader)
    leader_positions: list[tuple[str, TokenResult]] = []
    for name, trades in leader_trades.items():
        leader_positions.extend((name, p) for p in per_token(list(trades)))
        # Every leader buy counts for attribution, priced or not.
        for trade in trades:
            if trade.side == "BUY":
                leader_buys.append((trade.seen_at, trade.mint, name))

    for position in positions:
        if position.first_buy_at is None:
            continue
        at = position.first_buy_at
        candidates = {
            leader for t, mint, leader in leader_buys
            if mint == position.mint
            and at - window_seconds <= t <= at + COPY_EARLY_TOLERANCE_SECONDS
        }
        if len(candidates) == 1:
            position.leader = candidates.pop()
            position.leader_match = "mint+time"
        elif candidates:
            position.leader_match = "ambiguous: " + ", ".join(sorted(candidates))

    copy_buys = [
        (p.first_buy_at, p.mint) for p in positions if p.first_buy_at is not None
    ]
    rows = []
    for name, position in leader_positions:
        if position.first_buy_at is None:
            continue
        at = position.first_buy_at
        copied = any(
            mint == position.mint
            and at - COPY_EARLY_TOLERANCE_SECONDS <= t <= at + window_seconds
            for t, mint in copy_buys
        )
        rows.append({**position.as_dict(), "leader": name, "copied": copied})
    return rows


def _leader_summary(rows: Sequence[dict]) -> dict:
    """Per leader: how their own trades did, split by whether CopyFomo copied."""
    out: dict[str, dict] = {}
    for row in rows:
        entry = out.setdefault(row["leader"], {
            "leader_buys": 0, "copied": 0, "skipped": 0,
            "copied_results": defaultdict(list), "skipped_results": defaultdict(list),
        })
        entry["leader_buys"] += 1
        bucket = "copied" if row["copied"] else "skipped"
        entry[bucket] += 1
        if row["status"] == "CLOSED" and row["realized"] is not None:
            spent = row["spent"] or 0.0
            ret = row["realized"] / spent if spent else None
            if ret is not None:
                entry[f"{bucket}_results"][row["currency"]].append(ret)
    for entry in out.values():
        for bucket in ("copied_results", "skipped_results"):
            entry[bucket] = {
                currency: {
                    "closed": len(values),
                    "avg_return": sum(values) / len(values),
                    "win_rate": sum(v > 0 for v in values) / len(values),
                }
                for currency, values in entry[bucket].items()
            }
    return out


def _week(epoch: float) -> str:
    year, week, _ = datetime.fromtimestamp(epoch, UTC).isocalendar()
    return f"{year}-W{week:02d}"


def _stats(values: list[float]) -> dict:
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v <= 0]
    return {
        "positions": len(values),
        "realized": sum(values),
        "win_rate": len(wins) / len(values) if values else None,
        "avg_win": sum(wins) / len(wins) if wins else None,
        "avg_loss": sum(losses) / len(losses) if losses else None,
        "per_position_ci95": bootstrap_mean_ci(values),
    }


def build_copyfomo_report(
    tokens: Sequence[TokenResult], leader_rows: Sequence[dict] = ()
) -> dict:
    closed = [t for t in tokens if t.status == "CLOSED"]
    by_currency: dict[str, list[float]] = defaultdict(list)
    weeks: dict[tuple[str, str], list[float]] = defaultdict(list)
    leaders: dict[tuple[str, str], list[float]] = defaultdict(list)
    for token in closed:
        currency = token.currency or "?"
        value = token.realized or 0.0
        by_currency[currency].append(value)
        weeks[(currency, _week(token.last_trade_at))].append(value)
        leaders[(currency, token.leader or "unattributed")].append(value)
    return {
        "closed_positions": len(closed),
        "open_positions": sum(t.status == "OPEN" for t in tokens),
        "unpriced_tokens": sum(t.status == "UNPRICED" for t in tokens),
        "currencies": {c: _stats(v) for c, v in sorted(by_currency.items())},
        "weeks": {
            f"{week} {currency}": {
                "positions": len(v),
                "realized": sum(v),
                "win_rate": sum(x > 0 for x in v) / len(v),
            }
            for (currency, week), v in sorted(weeks.items(), key=lambda kv: kv[0][1])
        },
        "leaders": {
            f"{leader} ({currency})": _stats(v)
            for (currency, leader), v in sorted(
                leaders.items(), key=lambda kv: -sum(kv[1])
            )
        },
        "ambiguous_positions": sum(
            1 for t in tokens if (t.leader_match or "").startswith("ambiguous")
        ),
        "leader_trades": _leader_summary(leader_rows),
        "tokens": [t.as_dict() for t in tokens],
    }


def render_copyfomo_report(report: dict, wallet: str) -> str:
    lines = [f"CopyFomo wallet {wallet[:6]}...{wallet[-4:]} (on-chain, after all fees)"]
    if not report["tokens"]:
        lines.append(
            "No trades recorded yet. Set FEED_COPYFOMO_WALLETS=true and "
            "COPYFOMO_SOLANA_WALLET, then let the bot run."
        )
        return "\n".join(lines)

    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value * 100:.0f}%"

    def amt(value: float | None, currency: str) -> str:
        if value is None:
            return "n/a"
        digits = 2 if currency == "USDC" else 4
        return f"{value:+.{digits}f} {currency}"

    lines.append(
        f"Closed {report['closed_positions']} | open {report['open_positions']} | "
        f"unpriced {report['unpriced_tokens']}"
    )
    for currency, s in report["currencies"].items():
        ci = s["per_position_ci95"]
        lines.append(
            f"Realized {amt(s['realized'], currency)} | win rate {pct(s['win_rate'])}"
            f" | avg win {amt(s['avg_win'], currency)}"
            f" | avg loss {amt(s['avg_loss'], currency)}"
            + (f" | per position 95% CI {ci[0]:+.3f} to {ci[1]:+.3f}" if ci else "")
        )
        if s["positions"] < 30:
            lines.append("  -> fewer than 30 closed positions: too early for a verdict")
        elif ci and ci[0] > 0:
            lines.append("  -> profitable after fees, and the 95% CI is above zero")
        elif ci and ci[1] < 0:
            lines.append("  -> losing after fees, and the 95% CI is below zero")
        else:
            lines.append("  -> indistinguishable from zero so far")

    if report["leaders"] and report["leader_trades"]:
        lines.append("\nCopyFomo's result by leader copied (closed positions):")
        for name, s in report["leaders"].items():
            currency = name.rsplit("(", 1)[-1].rstrip(")")
            lines.append(
                f"  {name:<28} n={s['positions']:<3} {amt(s['realized'], currency)}"
                f"  win {pct(s['win_rate'])}"
            )
        if report["ambiguous_positions"]:
            lines.append(
                f"  {report['ambiguous_positions']} positions matched more than one "
                "leader and are left unattributed"
            )

    if report["leader_trades"]:
        lines.append(
            "\nLeaders' own trades (from their wallets), copied vs skipped by CopyFomo:"
        )
        for leader, data in report["leader_trades"].items():
            lines.append(
                f"  {leader:<16} buys {data['leader_buys']:<4} "
                f"copied {data['copied']:<4} skipped {data['skipped']}"
            )
            for bucket in ("copied_results", "skipped_results"):
                for currency, r in data[bucket].items():
                    lines.append(
                        f"    {bucket.split('_')[0]:<8} closed {r['closed']:<4} "
                        f"avg return {r['avg_return'] * 100:+.0f}%  "
                        f"win {pct(r['win_rate'])} ({currency})"
                    )
    elif report["tokens"]:
        lines.append(
            "\nNo leader wallets configured: set COPYFOMO_LEADER_WALLETS to see "
            "results per leader and the trades CopyFomo skipped."
        )

    if report["weeks"]:
        lines.append("\nBy week (positions closed that week):")
        for week, data in report["weeks"].items():
            currency = week.rsplit(" ", 1)[-1]
            lines.append(
                f"  {week:<14} n={data['positions']:<3} "
                f"{amt(data['realized'], currency)}  win {pct(data['win_rate'])}"
            )

    lines.append("\nPositions (latest last):")
    for token in report["tokens"][-25:]:
        currency = token["currency"] or ""
        if token["status"] == "CLOSED":
            detail = amt(token["realized"], currency)
        elif token["status"] == "OPEN":
            detail = (
                f"cost {token['spent']:.2f} {currency}, "
                f"{token['tokens_sold'] / token['tokens_bought'] * 100:.0f}% sold"
            )
        else:
            detail = "; ".join(token["problems"])
        leader = f" [{token['leader']}]" if token.get("leader") else ""
        lines.append(
            f"  {token['symbol'][:12]:<12} {token['status']:<8} {detail}{leader}"
        )
    return "\n".join(lines)
