"""Point-in-time replay of the price-volume engine against recorded signals.

Inputs, both already written by the bot:
  - launch_guard.db  intelligence_scores: one row per ~30s board scoring, whose
    first reason carries liquidity, market cap, m5 buys/sells and m5 volume.
    Market cap stands in for price (fixed supply), so only ratios are used.
  - launch_guard_outcomes.db: the signals and their post-signal price paths.

For each signal the engine sees only score rows strictly before the signal
time (no look-ahead). Trades are simulated with the evaluator's own ladder
and cost model, so "current" and "current + price-volume" differ only in
which signals are taken (entry test) or when a held trade is closed (exit
test).
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .evaluation import (
    CostModel,
    LadderRules,
    Summary,
    TrackedDecision,
    TradeResult,
    entry_observation,
    simulate_ladder_trade,
    summarize,
)
from .price_volume import ENTRY_STATES, PriceVolumeConfig, Sample, assess

_SCORE_LINE = re.compile(
    r"liquidity=\$(?P<liq>[\d,.]+), market_cap=\$(?P<mcap>[\d,.]+), "
    r"buys/sells=(?P<buys>\d+)/(?P<sells>\d+), volume5m=\$(?P<vol>[\d,.]+)"
)
EXIT_ACTIONS = {"strict": {"EXIT_REVIEW"}, "normal": {"REDUCE", "EXIT_REVIEW"}}


def _epoch(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def parse_score_row(scored_at: str, reasons_json: str) -> Sample | None:
    try:
        reasons = json.loads(reasons_json)
        match = _SCORE_LINE.search(str(reasons[0])) if reasons else None
        if match is None:
            return None
        num = lambda key: float(match[key].replace(",", ""))  # noqa: E731
        mcap = num("mcap")
        if mcap <= 0:
            return None
        return Sample(
            at=_epoch(scored_at),
            price=mcap,
            volume_m5_usd=num("vol"),
            buys_m5=int(match["buys"]),
            sells_m5=int(match["sells"]),
            liquidity_usd=num("liq"),
        )
    except (ValueError, TypeError, IndexError, json.JSONDecodeError):
        return None


def load_samples(
    launch_db: str | Path, mints: Iterable[str]
) -> dict[str, list[Sample]]:
    """Volume history for the given mints. Prefers the board's own polls
    (`board_samples`, real price); a mint with none falls back to
    `intelligence_scores` (market cap as the price proxy), or a `scores`
    table with the same columns (an extract)."""
    wanted = set(mints)
    connection = sqlite3.connect(f"file:{launch_db}?mode=ro", uri=True)
    out: dict[str, list[Sample]] = defaultdict(list)
    try:
        tables = {
            r[0]
            for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "board_samples" in tables:
            for row in connection.execute(
                "SELECT mint, at, price, volume_m5_usd, buys_m5, sells_m5, "
                "liquidity_usd FROM board_samples"
            ):
                if row[0] in wanted:
                    out[row[0]].append(
                        Sample(
                            at=row[1],
                            price=row[2],
                            volume_m5_usd=row[3],
                            buys_m5=int(row[4]),
                            sells_m5=int(row[5]),
                            liquidity_usd=row[6],
                        )
                    )
        from_board = set(out)
        table = next(
            (t for t in ("intelligence_scores", "scores") if t in tables), None
        )
        if table is not None:
            for scored_at, mint, reasons_json in connection.execute(
                f"SELECT scored_at, mint, reasons_json FROM {table}"
            ):
                if mint in wanted and mint not in from_board:
                    sample = parse_score_row(scored_at, reasons_json)
                    if sample is not None:
                        out[mint].append(sample)
    finally:
        connection.close()
    for rows in out.values():
        rows.sort(key=lambda s: s.at)
    return dict(out)


def _before(rows: Sequence[Sample], at: float) -> list[Sample]:
    return [s for s in rows if s.at < at]


@dataclass(slots=True)
class Row:
    decision: TrackedDecision
    state: str
    eligible: bool
    vetoed: bool
    score: float | None
    baseline: TradeResult | None
    overlay: dict[str, float | None]


def _overlay_pnl(
    decision: TrackedDecision,
    base: TradeResult,
    samples: Sequence[Sample],
    cfg: PriceVolumeConfig,
    rules: LadderRules,
    costs: CostModel,
    actions: set[str],
) -> float | None:
    """Close the whole position at the first observation where the engine,
    using score rows up to that moment, recommends one of `actions` and
    that moment precedes the baseline exit. Ladder legs already taken before
    then are ignored, so this flatters neither side much on small stakes."""
    found = entry_observation(decision, rules.entry_rules())
    if found is None:
        return None
    index, entry = found
    baseline_exit_at = base.entered_at + base.held_seconds
    for obs in decision.observations[index + 1 :]:
        if obs.observed_at >= baseline_exit_at:
            break
        if not obs.usable(rules.min_exit_liquidity_usd):
            continue
        history = [s for s in samples if s.at <= obs.observed_at]
        if not history or obs.observed_at - history[-1].at > 120:
            continue  # no fresh volume data at this moment
        if assess(history, cfg).exit_action in actions:
            return costs.net_pnl(entry.price_usd or 0.0, obs.price_usd or 0.0)
    return base.pnl_usd


def replay(
    decisions: Sequence[TrackedDecision],
    samples: dict[str, list[Sample]],
    cfg: PriceVolumeConfig,
    rules: LadderRules,
    costs: CostModel,
    *,
    exits: bool = True,
) -> list[Row]:
    rows: list[Row] = []
    for decision in decisions:
        history = _before(samples.get(decision.mint, []), decision.decided_at)
        recent = history and decision.decided_at - history[-1].at <= 120
        a = assess(history, cfg) if recent else None
        base = simulate_ladder_trade(decision, rules, costs)
        overlay: dict[str, float | None] = {}
        if exits and base is not None:
            for name, actions in EXIT_ACTIONS.items():
                overlay[name] = _overlay_pnl(
                    decision,
                    base,
                    samples.get(decision.mint, []),
                    cfg,
                    rules,
                    costs,
                    actions,
                )
        rows.append(
            Row(
                decision=decision,
                state=a.state if a else "NO_DATA",
                eligible=bool(a and a.entry_eligible),
                vetoed=bool(a.vetoed) if a else cfg.block_unknown,
                score=a.score if a else None,
                baseline=base,
                overlay=overlay,
            )
        )
    return rows


def _stats(results: Sequence[TradeResult]) -> dict:
    s: Summary = summarize(results)
    out = s.as_dict()
    returns = [r.pnl_usd / 5.0 * 100 for r in results]
    out["median_return_pct"] = statistics.median(returns) if returns else None
    out["avg_hold_minutes"] = (
        statistics.mean(r.held_seconds for r in results) / 60 if results else None
    )
    return out


def _pnl_stats(pnls: Sequence[float]) -> dict:
    if not pnls:
        return {"trades": 0}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_loss = -sum(losses)
    return {
        "trades": len(pnls),
        "win_rate": len(wins) / len(pnls),
        "expectancy_usd": sum(pnls) / len(pnls),
        "total_pnl_usd": sum(pnls),
        "profit_factor": sum(wins) / gross_loss if gross_loss > 0 else None,
    }


def build(rows: Sequence[Row], *, train_fraction: float = 0.7) -> dict:
    ordered = sorted(rows, key=lambda r: r.decision.decided_at)
    cutoff = (
        ordered[int(len(ordered) * train_fraction)].decision.decided_at
        if ordered
        else 0.0
    )
    report: dict = {"cutoff": cutoff, "signals": {}, "coverage": {}}
    by_label: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by_label[r.decision.label].append(r)
    by_label["ALL"] = list(rows)
    for label, members in sorted(by_label.items()):
        traded = [r for r in members if r.baseline is not None]
        covered = [r for r in traded if r.state != "NO_DATA"]
        section: dict = {}
        for split, keep in (
            ("all", lambda r: True),
            ("test", lambda r: r.decision.decided_at >= cutoff),
        ):
            pool = [r for r in covered if keep(r)]
            section[split] = {
                "current": _stats([r.baseline for r in pool if r.baseline]),
                "pv_confirm_mode": _stats(
                    [r.baseline for r in pool if r.eligible and r.baseline]
                ),
                "pv_veto_mode": _stats(
                    [r.baseline for r in pool if not r.vetoed and r.baseline]
                ),
                "pv_vetoed_trades": _stats(
                    [r.baseline for r in pool if r.vetoed and r.baseline]
                ),
                "with_pv_exit_normal": _pnl_stats(
                    [
                        pnl
                        for r in pool
                        if (pnl := r.overlay.get("normal")) is not None
                    ]
                ),
                "with_pv_exit_strict": _pnl_stats(
                    [
                        pnl
                        for r in pool
                        if (pnl := r.overlay.get("strict")) is not None
                    ]
                ),
            }
        states: dict[str, list[TradeResult]] = defaultdict(list)
        for r in covered:
            if r.baseline:
                states[r.state].append(r.baseline)
        section["by_state"] = {k: _stats(v) for k, v in sorted(states.items())}
        dips = states.get("HEALTHY_DIP_CONFIRMED", []) + states.get(
            "HEALTHY_DIP_CANDIDATE", []
        )
        section["false_healthy_dip_rate"] = (
            sum(t.pnl_usd <= 0 for t in dips) / len(dips) if dips else None
        )
        report["signals"][label] = section
        report["coverage"][label] = {
            "signals": len(members),
            "simulated": len(traded),
            "with_volume_history": len(covered),
        }
    return report


def _line(name: str, s: dict) -> str:
    if not s.get("trades"):
        return f"    {name:<22} n=0"
    win = s.get("win_rate")
    exp = s.get("expectancy_usd")
    pf = s.get("profit_factor")
    ci = s.get("expectancy_ci95_usd")
    med = s.get("median_return_pct")
    bits = [
        f"n={s['trades']:<4}",
        f"win={win * 100:4.0f}%" if win is not None else "win=  n/a",
        f"per trade ${exp:+.3f}" if exp is not None else "",
        f"[CI {ci[0]:+.3f} to {ci[1]:+.3f}]" if ci else "",
        f"PF {pf:.2f}" if pf is not None else "PF n/a",
        f"total ${s.get('total_pnl_usd', 0):+.2f}",
    ]
    if med is not None:
        bits.append(f"median {med:+.1f}%")
    if s.get("max_drawdown_usd") is not None:
        bits.append(f"maxDD ${s['max_drawdown_usd']:.2f}")
    return f"    {name:<22} " + "  ".join(b for b in bits if b)


def render(report: dict) -> str:
    out = [
        "Price-volume engine replay (signals scored on volume history strictly "
        "before each signal; same ladder exits and costs as `report`).",
        "",
    ]
    for label, section in report["signals"].items():
        cov = report["coverage"][label]
        out.append(
            f"{label}: {cov['signals']} signals, {cov['simulated']} simulated, "
            f"{cov['with_volume_history']} with volume history"
        )
        for split in ("all", "test"):
            out.append(f"  {split}:")
            for name, stats in section[split].items():
                out.append(_line(name, stats))
        out.append("  by state at signal time (all):")
        for state, stats in section["by_state"].items():
            out.append(_line(state, stats))
        if section["false_healthy_dip_rate"] is not None:
            out.append(
                f"  healthy-dip signals that lost: "
                f"{section['false_healthy_dip_rate']:.0%}"
            )
        out.append("")
    out.append(
        "Read with care: a filter is worth enabling only if its test-split "
        "per-trade result beats `current` with intervals that do not "
        "overlap, on 100+ test trades."
    )
    return "\n".join(out)


def entry_states() -> frozenset[str]:
    return ENTRY_STATES
