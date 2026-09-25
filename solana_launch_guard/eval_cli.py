"""launch-guard-eval: record outcomes, then judge strategies on them.

    launch-guard-eval track            # run beside the bot; read-only on its DBs
    launch-guard-eval report           # offline; no network
    launch-guard-eval report --json    # machine-readable
    launch-guard-eval copyfomo         # CopyFomo's realized SOL P&L, offline
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
from collections import defaultdict
from collections.abc import Sequence

from .copyfomo_report import (
    build_copyfomo_report,
    load_trades,
    per_token,
    render_copyfomo_report,
)
from .evaluation import (
    CostModel,
    ExitRules,
    Summary,
    TrackedDecision,
    TradeResult,
    horizon_stats,
    reason_category,
    simulate_trade,
    summarize,
    time_split,
)
from .outcome_tracker import (
    DEFAULT_HORIZONS,
    DexScreenerBatchClient,
    OutcomeStore,
    OutcomeTracker,
    TrackerConfig,
)

DEFAULT_OUTCOMES_DB = "launch_guard_outcomes.db"
MIN_TRADES_FOR_A_VERDICT = 100


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="launch-guard-eval")
    parser.add_argument("--outcomes-db", default=DEFAULT_OUTCOMES_DB)
    sub = parser.add_subparsers(dest="command", required=True)

    track = sub.add_parser("track", help="record post-decision prices")
    track.add_argument(
        "--launch-db", default=os.getenv("DATABASE_PATH", "launch_guard.db")
    )
    track.add_argument(
        "--ledger-db",
        default=os.getenv(
            "AGENT_LIVE_TRIAL_LEDGER_PATH", "launch_guard_live_trial.sqlite"
        ),
    )
    track.add_argument("--reject-sample-rate", type=float, default=0.10)
    track.add_argument("--dense-interval", type=float, default=60.0)
    track.add_argument("--dense-window", type=float, default=3600.0)
    track.add_argument("--max-decision-lag", type=float, default=300.0)
    track.add_argument("--requests-per-cycle", type=int, default=10)
    track.add_argument("--poll-seconds", type=float, default=20.0)

    report = sub.add_parser("report", help="simulate trades after costs")
    report.add_argument("--position-usd", type=float, default=5.0)
    report.add_argument("--slippage-bps", type=float, default=300.0)
    report.add_argument("--fee-bps", type=float, default=10.0)
    report.add_argument("--fixed-fee-usd", type=float, default=0.02)
    report.add_argument("--take-profit-pct", type=float, default=30.0)
    report.add_argument("--stop-loss-pct", type=float, default=20.0)
    report.add_argument("--max-hold-seconds", type=float, default=3600.0)
    report.add_argument("--min-exit-liquidity-usd", type=float, default=1000.0)
    report.add_argument("--dead-exit-fraction", type=float, default=0.0)
    report.add_argument("--max-entry-lag", type=float, default=300.0)
    report.add_argument("--train-fraction", type=float, default=0.7)
    report.add_argument("--min-category-size", type=int, default=20)
    report.add_argument("--json", action="store_true")

    copyfomo = sub.add_parser(
        "copyfomo", help="CopyFomo's realized P&L from its recorded wallet trades"
    )
    copyfomo.add_argument(
        "--db", default=os.getenv("DATABASE_PATH", "launch_guard.db")
    )
    copyfomo.add_argument(
        "--wallet", default=os.getenv("COPYFOMO_SOLANA_WALLET", "")
    )
    copyfomo.add_argument("--json", action="store_true")
    return parser


def _fmt_money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:+.3f}"


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _summary_line(name: str, summary: Summary) -> str:
    ci = summary.expectancy_ci95_usd
    ci_text = f" [95% CI {ci[0]:+.3f} to {ci[1]:+.3f}]" if ci else ""
    return (
        f"  {name:<10} n={summary.trades:<5} win={_fmt_pct(summary.win_rate):>4}  "
        f"avg win {_fmt_money(summary.avg_win_usd)}  "
        f"avg loss {_fmt_money(summary.avg_loss_usd)}  "
        f"per trade {_fmt_money(summary.expectancy_usd)}{ci_text}  "
        f"total {_fmt_money(summary.total_pnl_usd)}  "
        f"max DD ${summary.max_drawdown_usd:.2f}"
    )


def _verdict(summary: Summary) -> str:
    if summary.trades < MIN_TRADES_FOR_A_VERDICT:
        return (
            f"  -> too few trades for a verdict "
            f"(have {summary.trades}, want {MIN_TRADES_FOR_A_VERDICT}+)"
        )
    ci = summary.expectancy_ci95_usd
    if ci and ci[0] > 0:
        return "  -> positive after costs, and the whole 95% CI is above zero"
    if ci and ci[1] < 0:
        return "  -> losing after costs, and the whole 95% CI is below zero"
    return "  -> indistinguishable from zero; not evidence of an edge"


def _simulate(
    decisions: Sequence[TrackedDecision], rules: ExitRules, costs: CostModel
) -> list[TradeResult]:
    return [
        result
        for decision in decisions
        if (result := simulate_trade(decision, rules, costs)) is not None
    ]


def build_report(
    decisions: Sequence[TrackedDecision],
    rules: ExitRules,
    costs: CostModel,
    *,
    train_fraction: float,
    min_category_size: int,
) -> dict:
    groups: dict[str, list[TrackedDecision]] = defaultdict(list)
    for decision in decisions:
        groups[f"{decision.source}:{decision.label}"].append(decision)

    group_reports = {}
    for name, members in sorted(groups.items()):
        train, test = time_split(members, train_fraction)
        group_reports[name] = {
            "tracked": len(members),
            "all": summarize(_simulate(members, rules, costs)).as_dict(),
            "train": summarize(_simulate(train, rules, costs)).as_dict(),
            "test": summarize(_simulate(test, rules, costs)).as_dict(),
            "horizons": [
                _horizon_dict(horizon_stats(members, h, rules))
                for h in DEFAULT_HORIZONS
            ],
        }

    by_reason: dict[str, list[TrackedDecision]] = defaultdict(list)
    for decision in groups.get("launch:REJECTED", []):
        for reason in {reason_category(r) for r in decision.reasons}:
            by_reason[reason].append(decision)
    accepted = summarize(_simulate(groups.get("launch:ACCEPTED", []), rules, costs))
    filters: list[dict] = []
    for reason, members in by_reason.items():
        if len(members) < min_category_size:
            continue
        summary = summarize(_simulate(members, rules, costs))
        filters.append(
            {
                "reason": reason,
                "tracked": len(members),
                "if_bought": summary.as_dict(),
                "per_trade_vs_accepted_usd": (
                    None
                    if summary.expectancy_usd is None
                    or accepted.expectancy_usd is None
                    else summary.expectancy_usd - accepted.expectancy_usd
                ),
            }
        )
    filters.sort(key=lambda f: f["if_bought"]["expectancy_usd"] or 0.0, reverse=True)

    return {
        "costs": {
            "position_usd": costs.position_usd,
            "slippage_bps_per_side": costs.slippage_bps_per_side,
            "fee_bps_per_side": costs.fee_bps_per_side,
            "fixed_fee_usd_per_side": costs.fixed_fee_usd_per_side,
            "breakeven_move_pct": costs.breakeven_move_pct(),
        },
        "rules": {
            "take_profit_pct": rules.take_profit_pct,
            "stop_loss_pct": rules.stop_loss_pct,
            "max_hold_seconds": rules.max_hold_seconds,
            "min_exit_liquidity_usd": rules.min_exit_liquidity_usd,
            "dead_exit_fraction": rules.dead_exit_fraction,
        },
        "accepted_ci95_usd": accepted.expectancy_ci95_usd,
        "groups": group_reports,
        "rejection_filters": filters,
    }


def _filter_verdict(
    rejected_ci: Sequence[float] | None, accepted_ci: Sequence[float] | None
) -> str:
    if not rejected_ci or not accepted_ci:
        return "not enough data"
    if rejected_ci[1] < accepted_ci[0]:
        return "saving money"
    if rejected_ci[0] > accepted_ci[1]:
        return "may be costing you winners"
    return "no clear difference"


def _horizon_dict(stats: object) -> dict:
    return {
        name: getattr(stats, name)
        for name in (
            "horizon_seconds", "tokens", "median_return_pct",
            "share_dead", "share_doubled_by_then",
        )
    }


def _render(report: dict) -> str:
    lines: list[str] = []
    c = report["costs"]
    lines.append(
        f"Cost model: ${c['position_usd']:.2f} per trade, "
        f"{c['slippage_bps_per_side']:.0f} bps slippage + {c['fee_bps_per_side']:.0f} "
        f"bps fees per side, ${c['fixed_fee_usd_per_side']:.3f} fixed per side"
    )
    lines.append(
        f"Break-even: price must rise {c['breakeven_move_pct']:.1f}% between entry "
        "and exit just to get the stake back.\n"
    )
    if not report["groups"]:
        lines.append("No tracked decisions yet. Run `launch-guard-eval track` first.")
        return "\n".join(lines)

    for name, group in report["groups"].items():
        lines.append(f"{name}  ({group['tracked']} tracked)")
        for part in ("all", "train", "test"):
            lines.append(_summary_line(part, Summary(**group[part])))
        lines.append(_verdict(Summary(**group["test"])))
        horizon_bits = []
        for h in group["horizons"]:
            if not h["tokens"]:
                continue
            label = (
                f"{h['horizon_seconds'] / 3600:g}h"
                if h["horizon_seconds"] >= 3600
                else f"{h['horizon_seconds'] / 60:g}m"
            )
            horizon_bits.append(
                f"{label}: median {h['median_return_pct']:+.0f}%, "
                f"dead {_fmt_pct(h['share_dead'])}, "
                f"hit 2x {_fmt_pct(h['share_doubled_by_then'])}"
            )
        if horizon_bits:
            lines.append("  " + " | ".join(horizon_bits))
        lines.append("")

    if report["rejection_filters"]:
        lines.append(
            "Rejection filters - what the rejected tokens would have made if "
            "bought under the same rules:"
        )
        for f in report["rejection_filters"]:
            summary = Summary(**f["if_bought"])
            delta = f["per_trade_vs_accepted_usd"]
            verdict = _filter_verdict(
                summary.expectancy_ci95_usd, report["accepted_ci95_usd"]
            )
            lines.append(
                f"  {f['reason'][:60]:<60} n={summary.trades:<4} "
                f"per trade {_fmt_money(summary.expectancy_usd)} "
                f"(vs accepted {_fmt_money(delta)}) -> {verdict}"
            )
        lines.append(
            "  Verdicts need non-overlapping 95% CIs; 'no clear difference' "
            "means keep collecting before changing that filter."
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.command == "copyfomo":
        if not args.wallet:
            raise SystemExit("Set COPYFOMO_SOLANA_WALLET or pass --wallet.")
        cf_report = build_copyfomo_report(per_token(load_trades(args.db, args.wallet)))
        print(
            json.dumps(cf_report, indent=2)
            if args.json
            else render_copyfomo_report(cf_report, args.wallet)
        )
        return
    store = OutcomeStore(args.outcomes_db)
    try:
        if args.command == "track":
            config = TrackerConfig(
                reject_sample_rate=args.reject_sample_rate,
                dense_interval_seconds=args.dense_interval,
                dense_window_seconds=args.dense_window,
                max_decision_lag_seconds=args.max_decision_lag,
                requests_per_cycle=args.requests_per_cycle,
            )
            tracker = OutcomeTracker(
                store, DexScreenerBatchClient(), config,
                launch_db=args.launch_db, ledger_db=args.ledger_db,
            )
            with contextlib.suppress(KeyboardInterrupt):
                asyncio.run(tracker.run(args.poll_seconds))
            return
        costs = CostModel(
            position_usd=args.position_usd,
            slippage_bps_per_side=args.slippage_bps,
            fee_bps_per_side=args.fee_bps,
            fixed_fee_usd_per_side=args.fixed_fee_usd,
        )
        rules = ExitRules(
            take_profit_pct=args.take_profit_pct,
            stop_loss_pct=args.stop_loss_pct,
            max_hold_seconds=args.max_hold_seconds,
            min_exit_liquidity_usd=args.min_exit_liquidity_usd,
            dead_exit_fraction=args.dead_exit_fraction,
            max_entry_lag_seconds=args.max_entry_lag,
        )
        report = build_report(
            store.load(), rules, costs,
            train_fraction=args.train_fraction,
            min_category_size=args.min_category_size,
        )
        print(json.dumps(report, indent=2) if args.json else _render(report))
    finally:
        store.close()


if __name__ == "__main__":
    main()
