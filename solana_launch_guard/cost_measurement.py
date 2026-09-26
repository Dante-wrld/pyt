"""Measure real trading costs so the evaluation cost model is not a guess.

Every simulation so far assumed 3% slippage per side (a 7.4% break-even).
For a $5 order in a pool with $50k+ liquidity that is likely far too harsh,
and it decides whether a signal looks like it loses money or breaks even.
Two measurements replace the guess:

1. Round-trip quotes. For recently signalled tokens, ask Jupiter what
   ``position_usd`` of USDC buys, then what selling exactly those tokens back
   returns. The gap is the full round-trip cost at that size: pool fees and
   price impact on both legs, routing included. Quote-only: nothing is
   signed or sent.
2. Realized slippage. For the bot's own confirmed buys, how the tokens
   actually received compare with the quote (auto_buy_executions). That is
   the cost on top of the quote from price moving between quote and landing.

Network fees are not measured here; the cost model's fixed fee covers them.
"""
from __future__ import annotations

import asyncio
import sqlite3
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6


class QuoteClient(Protocol):
    async def order(
        self, *, input_mint: str, output_mint: str = ..., amount_raw: int,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class RoundTrip:
    mint: str
    symbol: str
    usd_in: float
    usd_back: float | None
    cost_pct: float | None
    buy_impact_pct: float | None
    sell_impact_pct: float | None
    error: str | None = None


def _impact(order: dict[str, Any]) -> float | None:
    raw = order.get("priceImpact")
    if raw is None:
        raw_pct = order.get("priceImpactPct")
        return None if raw_pct is None else float(raw_pct) * 100
    return float(raw)


async def measure_round_trip(
    client: QuoteClient, mint: str, symbol: str, usd: float
) -> RoundTrip:
    amount_raw = round(usd * 10**USDC_DECIMALS)
    try:
        buy = await client.order(input_mint=USDC_MINT, output_mint=mint,
                                 amount_raw=amount_raw)
        tokens = int(buy.get("outAmount") or 0)
        if tokens <= 0:
            return RoundTrip(mint, symbol, usd, None, None, None, None,
                             "no buy route")
        sell = await client.order(input_mint=mint, output_mint=USDC_MINT,
                                  amount_raw=tokens)
        back_raw = int(sell.get("outAmount") or 0)
        if back_raw <= 0:
            return RoundTrip(mint, symbol, usd, None, None, _impact(buy), None,
                             "no sell route")
    except (ConnectionError, OSError, ValueError, TypeError) as exc:
        return RoundTrip(mint, symbol, usd, None, None, None, None, str(exc)[:120])
    back = back_raw / 10**USDC_DECIMALS
    return RoundTrip(
        mint, symbol, usd, back, (1 - back / usd) * 100, _impact(buy), _impact(sell),
    )


def recent_signal_mints(db_path: str | Path, limit: int) -> list[tuple[str, str]]:
    """Most recently signalled distinct Solana mints, newest first (read-only)."""
    path = Path(db_path)
    if not path.exists():
        return []
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT mint, symbol FROM buy_signals WHERE chain = 'solana' "
            "GROUP BY mint ORDER BY MAX(id) DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()
    return [(str(m), str(s or m[:6])) for m, s in rows]


def realized_buy_slippage(db_path: str | Path) -> list[float]:
    """Percent shortfall of tokens received versus the quote, per confirmed
    buy (negative = received more than quoted). Read-only."""
    path = Path(db_path)
    if not path.exists():
        return []
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT expected_output_raw, actual_output_raw FROM auto_buy_executions "
            "WHERE expected_output_raw > 0 AND actual_output_raw > 0"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()
    return [(1 - actual / expected) * 100 for expected, actual in rows]


def _quantile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def summarize_costs(
    trips: Sequence[RoundTrip], slippage: Sequence[float]
) -> dict[str, Any]:
    costs = [t.cost_pct for t in trips if t.cost_pct is not None]
    median_rt = statistics.median(costs) if costs else None
    median_slip = statistics.median(slippage) if slippage else None
    # Per side, in basis points: half the quoted round trip, plus realized
    # slippage beyond the quote (never credited when fills beat the quote).
    per_side_bps = None
    if median_rt is not None:
        per_side_bps = median_rt / 2 * 100 + max(0.0, median_slip or 0.0) * 100
    return {
        "quoted": len(costs),
        "failed": [asdict(t) for t in trips if t.cost_pct is None],
        "round_trip_median_pct": median_rt,
        "round_trip_p75_pct": _quantile(costs, 0.75),
        "round_trip_max_pct": max(costs) if costs else None,
        "realized_buys": len(slippage),
        "realized_slippage_median_pct": median_slip,
        "suggested_slippage_bps_per_side": per_side_bps,
        "trips": [asdict(t) for t in trips],
    }


def render_costs(summary: dict[str, Any], *, usd: float) -> str:
    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.2f}%"

    lines = [f"Round-trip cost at ${usd:g} (quotes only, nothing sent)"]
    if summary["quoted"]:
        lines.append(
            f"  {summary['quoted']} tokens: "
            f"median {pct(summary['round_trip_median_pct'])}"
            f", 75th pct {pct(summary['round_trip_p75_pct'])}"
            f", worst {pct(summary['round_trip_max_pct'])}"
        )
        for trip in summary["trips"]:
            if trip["cost_pct"] is not None:
                lines.append(
                    f"    {trip['symbol'][:12]:<12} {trip['cost_pct']:6.2f}%"
                    f"  (impact in {pct(trip['buy_impact_pct'])},"
                    f" out {pct(trip['sell_impact_pct'])})"
                )
    else:
        lines.append("  no quotes (no signalled tokens, or no Jupiter key)")
    for failure in summary["failed"]:
        lines.append(f"    {failure['symbol'][:12]:<12} failed: {failure['error']}")

    lines.append(
        f"\nRealized slippage vs quote on {summary['realized_buys']} confirmed buys: "
        f"median {pct(summary['realized_slippage_median_pct'])}"
    )
    bps = summary["suggested_slippage_bps_per_side"]
    if bps is not None:
        lines.append(
            f"\nSuggested cost model: --slippage-bps {bps:.0f} --fee-bps 0 "
            "(quotes already include pool fees; keep --fixed-fee-usd for network fees)"
        )
        lines.append(
            "Compare with the default 300 bps per side. Re-measure now and then: "
            "costs change with liquidity and token mix."
        )
    return "\n".join(lines)


async def measure_costs(
    client: QuoteClient | None,
    mints: Sequence[tuple[str, str]],
    *,
    usd: float,
    pause_seconds: float = 1.1,
) -> list[RoundTrip]:
    trips: list[RoundTrip] = []
    if client is None:
        return trips
    for index, (mint, symbol) in enumerate(mints):
        if index:
            await asyncio.sleep(pause_seconds)  # stay under Jupiter's rate limit
        trips.append(await measure_round_trip(client, mint, symbol, usd))
    return trips
