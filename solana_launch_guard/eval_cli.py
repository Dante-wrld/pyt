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
import itertools
import json
import logging
import os
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace

from .config import _load_dotenv
from .copyfomo_report import (
    build_copyfomo_report,
    load_trades,
    per_token,
    render_copyfomo_report,
)
from .evaluation import (
    CostModel,
    ExitRules,
    LadderRules,
    Summary,
    TrackedDecision,
    TradeResult,
    excursion_stats,
    horizon_stats,
    reason_category,
    simulate_ladder_trade,
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


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _add_ladder_args(parser: argparse.ArgumentParser) -> None:
    """Ladder settings default to the live .env values (same names)."""
    add = parser.add_argument
    add("--ladder-stop-loss-pct", type=float,
        default=_env_float("STOP_LOSS_PCT", 20.0),
        help="price-only stand-in for the live reversal exits")
    add("--principal-multiple", type=float,
        default=_env_float("AUTO_SELL_PRINCIPAL_MULTIPLE", 2.0))
    add("--half-profit-multiple", type=float,
        default=_env_float("AUTO_SELL_HALF_PROFIT_MULTIPLE", 3.0))
    add("--second-stage-fraction", type=float,
        default=_env_float("AUTO_SELL_SECOND_STAGE_FRACTION", 0.5))
    add("--trailing-activation-pct", type=float,
        default=_env_float("TRAILING_ACTIVATION_PCT", 20.0))
    add("--trailing-stop-pct", type=float,
        default=_env_float("TRAILING_STOP_PCT", 12.0))
    add("--principal-trailing-stop-pct", type=float,
        default=_env_float("PRINCIPAL_RECOVERED_TRAILING_STOP_PCT", 25.0))
    add("--stagnation-window", type=float,
        default=_env_float("STAGNATION_WINDOW_SECONDS", 300.0),
        help="seconds; 0 turns the stagnation exit off")
    add("--stagnation-min-gain-pct", type=float,
        default=_env_float("STAGNATION_MIN_GAIN_PCT", 3.0))
    add("--ladder-max-hold-seconds", type=float, default=24 * 3600.0)


def _add_cost_args(parser: argparse.ArgumentParser) -> None:
    add = parser.add_argument
    add("--position-usd", type=float, default=5.0)
    add("--slippage-bps", type=float, default=300.0)
    add("--fee-bps", type=float, default=10.0)
    add("--fixed-fee-usd", type=float, default=0.02)
    add("--min-exit-liquidity-usd", type=float, default=1000.0)
    add("--dead-exit-fraction", type=float, default=0.0)
    add("--max-entry-lag", type=float, default=300.0)


def _costs(args: argparse.Namespace) -> CostModel:
    return CostModel(
        position_usd=args.position_usd,
        slippage_bps_per_side=args.slippage_bps,
        fee_bps_per_side=args.fee_bps,
        fixed_fee_usd_per_side=args.fixed_fee_usd,
    )


def _ladder(args: argparse.Namespace) -> LadderRules:
    return LadderRules(
        stop_loss_pct=args.ladder_stop_loss_pct,
        principal_multiple=args.principal_multiple,
        half_profit_multiple=args.half_profit_multiple,
        second_stage_fraction=args.second_stage_fraction,
        trailing_activation_pct=args.trailing_activation_pct,
        trailing_stop_pct=args.trailing_stop_pct,
        principal_recovered_trailing_stop_pct=args.principal_trailing_stop_pct,
        stagnation_window_seconds=args.stagnation_window,
        stagnation_min_gain_pct=args.stagnation_min_gain_pct,
        stagnation_enabled=args.stagnation_window > 0,
        max_hold_seconds=args.ladder_max_hold_seconds,
        min_exit_liquidity_usd=args.min_exit_liquidity_usd,
        dead_exit_fraction=args.dead_exit_fraction,
        max_entry_lag_seconds=args.max_entry_lag,
    )


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
    _add_cost_args(report)
    report.add_argument(
        "--exit-model", choices=("simple", "ladder"), default="simple",
        help="simple: one TP/SL/time exit; ladder: the live-style profit ladder",
    )
    report.add_argument("--take-profit-pct", type=float, default=30.0)
    report.add_argument("--stop-loss-pct", type=float, default=20.0)
    report.add_argument("--max-hold-seconds", type=float, default=3600.0)
    _add_ladder_args(report)
    report.add_argument("--train-fraction", type=float, default=0.7)
    report.add_argument("--min-category-size", type=int, default=20)
    report.add_argument(
        "--group", default="",
        help="only groups starting with this, e.g. 'signal:' or 'signal:MOMENTUM BUY'",
    )
    report.add_argument("--json", action="store_true")

    sweep = sub.add_parser(
        "sweep", help="try many ladder exit settings: rank on train, show test"
    )
    _add_cost_args(sweep)
    _add_ladder_args(sweep)
    sweep.add_argument("--group", default="signal:MOMENTUM BUY")
    sweep.add_argument("--train-fraction", type=float, default=0.7)
    sweep.add_argument("--stops", default="10,15,20,30")
    sweep.add_argument("--trails", default="8,12,20")
    sweep.add_argument("--activations", default="10,20,40")
    sweep.add_argument("--principal-multiples", default="1.5,2,3")
    sweep.add_argument("--stagnation-windows", default="0,300,900")
    sweep.add_argument("--min-train-trades", type=int, default=30)
    sweep.add_argument("--top", type=int, default=10)
    sweep.add_argument("--json", action="store_true")

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
    decisions: Sequence[TrackedDecision],
    rules: ExitRules | LadderRules,
    costs: CostModel,
) -> list[TradeResult]:
    results: list[TradeResult] = []
    for decision in decisions:
        result = (
            simulate_ladder_trade(decision, rules, costs)
            if isinstance(rules, LadderRules)
            else simulate_trade(decision, rules, costs)
        )
        if result is not None:
            results.append(result)
    return results


def _entry_rules(rules: ExitRules | LadderRules) -> ExitRules:
    return rules.entry_rules() if isinstance(rules, LadderRules) else rules


def build_report(
    decisions: Sequence[TrackedDecision],
    rules: ExitRules | LadderRules,
    costs: CostModel,
    *,
    train_fraction: float,
    min_category_size: int,
) -> dict:
    groups: dict[str, list[TrackedDecision]] = defaultdict(list)
    for decision in decisions:
        groups[f"{decision.source}:{decision.label}"].append(decision)

    group_reports: dict[str, dict] = {}
    for name, members in sorted(groups.items()):
        train, test = time_split(members, train_fraction)
        group_reports[name] = {
            "tracked": len(members),
            "all": summarize(_simulate(members, rules, costs)).as_dict(),
            "train": summarize(_simulate(train, rules, costs)).as_dict(),
            "test": summarize(_simulate(test, rules, costs)).as_dict(),
            "excursions": excursion_stats(
                members, _entry_rules(rules),
                breakeven_pct=costs.breakeven_move_pct(),
                stop_pct=rules.stop_loss_pct,
            ),
            "horizons": [
                _horizon_dict(horizon_stats(members, h, _entry_rules(rules)))
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

    patterns: dict[str, list[dict]] = {}
    for name, members in sorted(groups.items()):
        if not name.startswith("signal:"):
            continue
        by_pattern: dict[str, list[TrackedDecision]] = defaultdict(list)
        for decision in members:
            tag = next(
                (r[len("candle: "):] for r in decision.reasons
                 if r.startswith("candle: ")),
                "untagged",
            )
            by_pattern[tag].append(decision)
        rows: list[dict] = []
        for tag, tagged in by_pattern.items():
            if len(tagged) < min_category_size:
                continue
            rows.append({
                "pattern": tag,
                "tracked": len(tagged),
                "summary": summarize(_simulate(tagged, rules, costs)).as_dict(),
            })
        rows.sort(key=lambda r: r["summary"]["expectancy_usd"] or 0.0, reverse=True)
        if rows:
            patterns[name] = rows

    momentum = group_reports.get("signal:MOMENTUM BUY", {}).get("test")
    pullback = group_reports.get("signal:BUY ZONE", {}).get("test")
    head_to_head = None
    if momentum and pullback:
        head_to_head = {
            "momentum_test": momentum,
            "buy_zone_test": pullback,
            "verdict": _promotion_verdict(Summary(**momentum), Summary(**pullback)),
        }

    return {
        "momentum_vs_buy_zone": head_to_head,
        "signal_candle_patterns": patterns,
        "costs": {
            "position_usd": costs.position_usd,
            "slippage_bps_per_side": costs.slippage_bps_per_side,
            "fee_bps_per_side": costs.fee_bps_per_side,
            "fixed_fee_usd_per_side": costs.fixed_fee_usd_per_side,
            "breakeven_move_pct": costs.breakeven_move_pct(),
        },
        "exit_model": "ladder" if isinstance(rules, LadderRules) else "simple",
        "rules": {
            name: getattr(rules, name) for name in rules.__dataclass_fields__
        },
        "accepted_ci95_usd": accepted.expectancy_ci95_usd,
        "groups": group_reports,
        "rejection_filters": filters,
    }


def _promotion_verdict(momentum: Summary, buy_zone: Summary) -> str:
    """The rule agreed before any data: MOMENTUM BUY goes live only with 100+
    test signals, a 95% CI above zero, and no worse than BUY ZONE."""
    if momentum.trades < MIN_TRADES_FOR_A_VERDICT:
        return (
            f"keep MOMENTUM BUY shadow-only: {momentum.trades} test signals, "
            f"need {MIN_TRADES_FOR_A_VERDICT}+"
        )
    ci = momentum.expectancy_ci95_usd
    if not ci or ci[0] <= 0:
        return "keep MOMENTUM BUY shadow-only: its 95% CI is not above zero"
    if (
        buy_zone.expectancy_usd is not None
        and momentum.expectancy_usd is not None
        and momentum.expectancy_usd < buy_zone.expectancy_usd
    ):
        return "keep MOMENTUM BUY shadow-only: positive, but worse than BUY ZONE"
    return (
        "MOMENTUM BUY meets the promotion rule: consider adding it to "
        "ENTRY_ALLOWED_DECISIONS"
    )


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
        exc = group.get("excursions") or {}
        if exc.get("tokens"):
            bits = []
            for t in exc["targets"]:
                label = (
                    f"break-even +{t['pct']:.1f}%"
                    if t["pct"] == exc["breakeven_pct"] else f"+{t['pct']:g}%"
                )
                first = _fmt_pct(t["reached_before_stop"])
                bits.append(
                    f"{label} {_fmt_pct(t['reached'])} "
                    f"({first} before -{exc['stop_pct']:g}%)"
                )
            lines.append("  upside reached: " + " | ".join(bits))
            lines.append(
                f"  median peak {exc['median_peak_pct']:+.0f}% after "
                f"{exc['median_minutes_to_peak']:.0f} min; "
                f"{_fmt_pct(exc['share_hit_stop'])} hit the stop at some point"
            )
        lines.append("")

    if report.get("signal_candle_patterns"):
        lines.append(
            "Signals by the candle they fired on (same costs and exits; "
            "'untagged' = before tagging existed):"
        )
        for name, rows in report["signal_candle_patterns"].items():
            lines.append(f"  {name}")
            for row in rows:
                summary = Summary(**row["summary"])
                lines.append(_summary_line(row["pattern"][:10], summary))
        lines.append(
            "  Compare patterns within one signal type only. A pattern earns a "
            "rule only if its CI clears the others'.\n"
        )

    if report.get("momentum_vs_buy_zone"):
        h2h = report["momentum_vs_buy_zone"]
        lines.append("MOMENTUM BUY vs BUY ZONE (test set, same costs and exits):")
        lines.append(_summary_line("momentum", Summary(**h2h["momentum_test"])))
        lines.append(_summary_line("buy zone", Summary(**h2h["buy_zone_test"])))
        lines.append(f"  -> {h2h['verdict']}\n")

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


def _floats(raw: str) -> list[float]:
    return [float(x) for x in raw.split(",") if x.strip()]


def run_sweep(
    members: Sequence[TrackedDecision],
    live: LadderRules,
    costs: CostModel,
    args: argparse.Namespace,
) -> dict:
    """Grid-search the ladder on the TRAIN part only, then show how the best
    settings did on TEST. Picking by test would make test meaningless."""
    train, test = time_split(members, args.train_fraction)
    rows: list[dict] = []
    grid = itertools.product(
        _floats(args.stops), _floats(args.trails), _floats(args.activations),
        _floats(args.principal_multiples), _floats(args.stagnation_windows),
    )
    for stop, trail, activation, principal, stagnation in grid:
        rules = replace(
            live, stop_loss_pct=stop, trailing_stop_pct=trail,
            trailing_activation_pct=activation, principal_multiple=principal,
            half_profit_multiple=max(live.half_profit_multiple, principal + 0.5),
            stagnation_window_seconds=stagnation,
            stagnation_enabled=stagnation > 0,
        )
        tr = summarize(_simulate(train, rules, costs))
        if tr.trades < args.min_train_trades:
            continue
        rows.append({
            "stop_loss_pct": stop, "trailing_stop_pct": trail,
            "trailing_activation_pct": activation, "principal_multiple": principal,
            "stagnation_window_seconds": stagnation,
            "train": tr.as_dict(),
            "test": summarize(_simulate(test, rules, costs)).as_dict(),
        })
    rows.sort(key=lambda r: r["train"]["expectancy_usd"] or 0.0, reverse=True)
    return {
        "group": args.group,
        "tracked": len(members),
        "train_tracked": len(train),
        "test_tracked": len(test),
        "combinations_kept": len(rows),
        "live_settings": {
            "train": summarize(_simulate(train, live, costs)).as_dict(),
            "test": summarize(_simulate(test, live, costs)).as_dict(),
        },
        "best": rows[: args.top],
    }


def _render_sweep(result: dict) -> str:
    lines = [
        f"Sweep over {result['group']}: {result['tracked']} tracked "
        f"({result['train_tracked']} train / {result['test_tracked']} test), "
        f"{result['combinations_kept']} settings with enough train trades",
    ]
    live = result["live_settings"]
    lines.append(_summary_line("live/train", Summary(**live["train"])))
    lines.append(_summary_line("live/test", Summary(**live["test"])))
    if not result["best"]:
        lines.append(
            "\nNot enough data yet. Keep `launch-guard-eval track` running; "
            "a sweep needs a few hundred signals to mean anything."
        )
        return "\n".join(lines)
    lines.append(
        "\nTop settings ranked by TRAIN (stop/trail/activation/principal/stag):"
    )
    for row in result["best"]:
        name = (
            f"{row['stop_loss_pct']:g}/{row['trailing_stop_pct']:g}/"
            f"{row['trailing_activation_pct']:g}/{row['principal_multiple']:g}x/"
            f"{row['stagnation_window_seconds']:g}s"
        )
        tr, te = Summary(**row["train"]), Summary(**row["test"])
        ci = te.expectancy_ci95_usd
        lines.append(
            f"  {name:<22} train {_fmt_money(tr.expectancy_usd)} (n={tr.trades})"
            f" | test {_fmt_money(te.expectancy_usd)} (n={te.trades})"
            + (f" CI {ci[0]:+.3f}..{ci[1]:+.3f}" if ci else "")
        )
    lines.append(
        "\nThe best train row usually looks better than it is. Trust a setting "
        "only if its test result holds up, and change live settings once, "
        "not after every sweep."
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    _load_dotenv()  # same settings the bot runs with, so defaults match live
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
        costs = _costs(args)
        if args.command == "sweep":
            members = [
                d for d in store.load()
                if f"{d.source}:{d.label}".startswith(args.group)
            ]
            result = run_sweep(members, _ladder(args), costs, args)
            print(json.dumps(result, indent=2) if args.json else _render_sweep(result))
            return
        rules: ExitRules | LadderRules = (
            _ladder(args) if args.exit_model == "ladder" else ExitRules(
            take_profit_pct=args.take_profit_pct,
            stop_loss_pct=args.stop_loss_pct,
            max_hold_seconds=args.max_hold_seconds,
            min_exit_liquidity_usd=args.min_exit_liquidity_usd,
            dead_exit_fraction=args.dead_exit_fraction,
            max_entry_lag_seconds=args.max_entry_lag,
        ))
        loaded = [
            d for d in store.load()
            if f"{d.source}:{d.label}".startswith(args.group)
        ]
        report = build_report(
            loaded, rules, costs,
            train_fraction=args.train_fraction,
            min_category_size=args.min_category_size,
        )
        print(json.dumps(report, indent=2) if args.json else _render(report))
    finally:
        store.close()


if __name__ == "__main__":
    main()
