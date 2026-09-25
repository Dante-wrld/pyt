"""Offline evaluation of recorded decisions against what prices did afterward.

Everything here is pure: it takes observations that the outcome tracker already
recorded and never touches the network, a wallet, or the live databases. The
point is to answer two questions with data instead of a week of live trades:

1. After every realistic cost, does a strategy have positive expectancy on
   data it was not tuned on?
2. Is each rejection filter saving money, or throwing away winners?

Conventions that keep the numbers honest rather than flattering:

- Entry is the first *observed* quote after the decision, not the launch
  price, so the latency the bot would really have is included.
- A take-profit fills at the lower of the observed price and the target; a
  stop-loss fills at the observed price even when it gapped far below the stop.
- A token whose quotes disappear or go illiquid and never come back is treated
  as unsellable (``dead_exit_fraction`` of entry, default 0).
- Train/test is a split in time: tune on the earlier part, judge on the later.
"""
from __future__ import annotations

import math
import random
import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True, slots=True)
class Observation:
    observed_at: float
    price_usd: float | None
    liquidity_usd: float | None
    found: bool

    def usable(self, min_liquidity_usd: float) -> bool:
        return (
            self.found
            and self.price_usd is not None
            and self.price_usd > 0
            and (self.liquidity_usd or 0.0) >= min_liquidity_usd
        )


@dataclass(frozen=True, slots=True)
class TrackedDecision:
    source: str
    mint: str
    decided_at: float
    label: str
    reasons: tuple[str, ...]
    observations: tuple[Observation, ...]


@dataclass(frozen=True, slots=True)
class CostModel:
    """Round-trip costs for one position. Defaults are deliberately not rosy
    for $2-5 orders in thin pump.fun/PumpSwap pools."""

    position_usd: float = 5.0
    slippage_bps_per_side: float = 300.0
    fee_bps_per_side: float = 10.0
    fixed_fee_usd_per_side: float = 0.02

    def net_pnl(self, entry_price: float, exit_price: float) -> float:
        side = (self.slippage_bps_per_side + self.fee_bps_per_side) / 10_000
        invested = self.position_usd - self.fixed_fee_usd_per_side
        if invested <= 0 or entry_price <= 0:
            return -self.position_usd
        tokens = invested * (1 - side) / entry_price
        proceeds = tokens * exit_price * (1 - side) - self.fixed_fee_usd_per_side
        # An exit worth less than the network fee would not be sent at all.
        return max(proceeds, 0.0) - self.position_usd

    def breakeven_move_pct(self) -> float:
        """Price rise needed between entry and exit just to get the stake back."""
        side = (self.slippage_bps_per_side + self.fee_bps_per_side) / 10_000
        invested = self.position_usd - self.fixed_fee_usd_per_side
        if invested <= 0 or side >= 1:
            return math.inf
        needed = (self.position_usd + self.fixed_fee_usd_per_side) / (
            invested * (1 - side) ** 2
        )
        return (needed - 1) * 100


@dataclass(frozen=True, slots=True)
class ExitRules:
    take_profit_pct: float = 30.0
    stop_loss_pct: float = 20.0
    max_hold_seconds: float = 3600.0
    min_exit_liquidity_usd: float = 1000.0
    dead_exit_fraction: float = 0.0
    max_entry_lag_seconds: float = 300.0


@dataclass(frozen=True, slots=True)
class TradeResult:
    mint: str
    label: str
    entered_at: float
    entry_price: float
    exit_price: float
    exit_reason: str
    held_seconds: float
    pnl_usd: float

    @property
    def gross_return_pct(self) -> float:
        return (self.exit_price / self.entry_price - 1) * 100


def entry_observation(
    decision: TrackedDecision, rules: ExitRules
) -> tuple[int, Observation] | None:
    for index, obs in enumerate(decision.observations):
        if obs.observed_at - decision.decided_at > rules.max_entry_lag_seconds:
            return None
        if obs.usable(rules.min_exit_liquidity_usd):
            return index, obs
    return None


def simulate_trade(
    decision: TrackedDecision, rules: ExitRules, costs: CostModel
) -> TradeResult | None:
    """Replay one hypothetical buy through the recorded price path.

    Returns None when no usable entry quote exists within the entry lag, so a
    token nobody could have bought never counts as a win or a loss.
    """
    entry = entry_observation(decision, rules)
    if entry is None:
        return None
    start, entry_obs = entry
    entry_price = float(entry_obs.price_usd or 0.0)
    take_profit = entry_price * (1 + rules.take_profit_pct / 100)
    stop_loss = entry_price * (1 - rules.stop_loss_pct / 100)
    after = decision.observations[start + 1 :]

    last_usable = -1
    for index, obs in enumerate(after):
        if obs.usable(rules.min_exit_liquidity_usd):
            last_usable = index

    exit_price: float | None = None
    exit_at = entry_obs.observed_at
    reason = "DATA_END"
    for index, obs in enumerate(after):
        if not obs.usable(rules.min_exit_liquidity_usd):
            continue
        price = float(obs.price_usd or 0.0)
        exit_at = obs.observed_at
        if price <= stop_loss:
            exit_price, reason = price, "STOP_LOSS"
            break
        if price >= take_profit:
            exit_price, reason = min(price, take_profit), "TAKE_PROFIT"
            break
        if obs.observed_at - entry_obs.observed_at >= rules.max_hold_seconds:
            exit_price, reason = price, "TIME_EXIT"
            break
        if index == last_usable:
            exit_price = price

    if reason == "DATA_END":
        trailing_dead = last_usable < len(after) - 1
        if trailing_dead:
            exit_price = entry_price * rules.dead_exit_fraction
            reason = "DIED"
            exit_at = after[-1].observed_at
        elif exit_price is None:
            exit_price = entry_price

    assert exit_price is not None
    return TradeResult(
        mint=decision.mint,
        label=decision.label,
        entered_at=entry_obs.observed_at,
        entry_price=entry_price,
        exit_price=exit_price,
        exit_reason=reason,
        held_seconds=max(0.0, exit_at - entry_obs.observed_at),
        pnl_usd=costs.net_pnl(entry_price, exit_price),
    )


@dataclass(frozen=True, slots=True)
class LadderRules:
    """Price-only model of the live exit (hunter_shadow_strategy.assess_exit
    plus the auto-sell profit ladder), using the same settings names.

    Modelled exactly: principal recovery at ``principal_multiple``, the second
    stage at ``half_profit_multiple``, the trailing stop that widens once the
    principal is back, the stagnation exit, and max hold.

    Approximated: the live bot's reversal exits also need momentum and
    buy/sell evidence that the tracker does not record. ``stop_loss_pct`` is
    the price-only stand-in for those, and the live trailing stop waits for
    selling pressure where this one fires on price alone.
    """

    stop_loss_pct: float = 20.0
    principal_multiple: float = 2.0
    half_profit_multiple: float = 3.0
    second_stage_fraction: float = 0.5
    trailing_activation_pct: float = 20.0
    trailing_stop_pct: float = 12.0
    principal_recovered_trailing_stop_pct: float = 25.0
    stagnation_window_seconds: float = 300.0
    stagnation_min_gain_pct: float = 3.0
    stagnation_enabled: bool = True
    max_hold_seconds: float = 24 * 3600.0
    min_exit_liquidity_usd: float = 1000.0
    dead_exit_fraction: float = 0.0
    max_entry_lag_seconds: float = 300.0

    def entry_rules(self) -> ExitRules:
        return ExitRules(
            min_exit_liquidity_usd=self.min_exit_liquidity_usd,
            max_entry_lag_seconds=self.max_entry_lag_seconds,
        )


def simulate_ladder_trade(
    decision: TrackedDecision, rules: LadderRules, costs: CostModel
) -> TradeResult | None:
    """Replay one buy through the live-style exit ladder, selling in legs.
    Each leg pays slippage, fees and the fixed network fee separately."""
    entry = entry_observation(decision, rules.entry_rules())
    if entry is None:
        return None
    start, entry_obs = entry
    entry_price = float(entry_obs.price_usd or 0.0)
    side = (costs.slippage_bps_per_side + costs.fee_bps_per_side) / 10_000
    invested = costs.position_usd - costs.fixed_fee_usd_per_side
    remaining = invested * (1 - side) / entry_price if invested > 0 else 0.0
    proceeds = 0.0
    sold_value = sold_tokens = 0.0
    legs: list[str] = []

    def sell(tokens: float, price: float, label: str) -> None:
        nonlocal remaining, proceeds, sold_value, sold_tokens
        tokens = min(tokens, remaining)
        if tokens <= 0:
            return
        leg = tokens * price * (1 - side) - costs.fixed_fee_usd_per_side
        proceeds += max(leg, 0.0)
        sold_value += tokens * price
        sold_tokens += tokens
        remaining -= tokens
        legs.append(label)

    after = decision.observations[start + 1 :]
    usable = [o for o in after if o.usable(rules.min_exit_liquidity_usd)]
    trailing_dead = bool(after) and not after[-1].usable(rules.min_exit_liquidity_usd)
    peak = entry_price
    principal_done = second_done = False
    exit_at = entry_obs.observed_at
    for obs in usable:
        price = float(obs.price_usd or 0.0)
        exit_at = obs.observed_at
        age = obs.observed_at - entry_obs.observed_at
        peak = max(peak, price)
        multiple = price / entry_price
        gain_pct = (multiple - 1) * 100
        peak_gain_pct = (peak / entry_price - 1) * 100
        drawdown_pct = (1 - price / peak) * 100
        trail = (
            rules.principal_recovered_trailing_stop_pct
            if principal_done else rules.trailing_stop_pct
        )
        stop_price = entry_price * (1 - rules.stop_loss_pct / 100)
        if not principal_done and price <= stop_price:
            sell(remaining, price, "STOP_LOSS")
        elif peak_gain_pct >= rules.trailing_activation_pct and drawdown_pct >= trail:
            sell(remaining, price, "TRAILING")
        elif (
            rules.stagnation_enabled
            and not principal_done
            and age >= rules.stagnation_window_seconds
            and gain_pct < rules.stagnation_min_gain_pct
        ):
            sell(remaining, price, "STAGNANT")
        elif age >= rules.max_hold_seconds:
            sell(remaining, price, "TIME_EXIT")
        elif not principal_done and multiple >= rules.principal_multiple:
            # Sell just enough to get the stake back after costs.
            needed = costs.position_usd + costs.fixed_fee_usd_per_side
            sell(needed / (price * (1 - side)), price, "PRINCIPAL")
            principal_done = True
        elif (
            principal_done
            and not second_done
            and multiple >= rules.half_profit_multiple
        ):
            sell(remaining * rules.second_stage_fraction, price, "SECOND_STAGE")
            second_done = True
        if remaining <= 0:
            break

    if remaining > 0:
        if trailing_dead:
            dead_price = entry_price * rules.dead_exit_fraction
            if dead_price > 0:
                sell(remaining, dead_price, "DIED")
            else:
                legs.append("DIED")
                remaining = 0.0
            exit_at = after[-1].observed_at
        else:
            last = float(usable[-1].price_usd or 0.0) if usable else entry_price
            sell(remaining, last, "DATA_END")

    return TradeResult(
        mint=decision.mint,
        label=decision.label,
        entered_at=entry_obs.observed_at,
        entry_price=entry_price,
        exit_price=sold_value / sold_tokens if sold_tokens else 0.0,
        exit_reason="+".join(dict.fromkeys(legs)) or "DATA_END",
        held_seconds=max(0.0, exit_at - entry_obs.observed_at),
        pnl_usd=proceeds - costs.position_usd,
    )


@dataclass(slots=True)
class Summary:
    trades: int
    win_rate: float | None = None
    avg_win_usd: float | None = None
    avg_loss_usd: float | None = None
    expectancy_usd: float | None = None
    expectancy_ci95_usd: tuple[float, float] | None = None
    total_pnl_usd: float = 0.0
    profit_factor: float | None = None
    max_drawdown_usd: float = 0.0
    exit_reasons: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def bootstrap_mean_ci(
    values: Sequence[float], *, resamples: int = 2000, seed: int = 7
) -> tuple[float, float] | None:
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    n = len(values)
    means = sorted(
        sum(values[rng.randrange(n)] for _ in range(n)) / n
        for _ in range(resamples)
    )
    return means[int(0.025 * resamples)], means[int(0.975 * resamples) - 1]


def summarize(results: Iterable[TradeResult]) -> Summary:
    ordered = sorted(results, key=lambda r: r.entered_at)
    summary = Summary(trades=len(ordered))
    if not ordered:
        return summary
    pnls = [r.pnl_usd for r in ordered]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    summary.win_rate = len(wins) / len(pnls)
    summary.avg_win_usd = sum(wins) / len(wins) if wins else None
    summary.avg_loss_usd = sum(losses) / len(losses) if losses else None
    summary.expectancy_usd = sum(pnls) / len(pnls)
    summary.expectancy_ci95_usd = bootstrap_mean_ci(pnls)
    summary.total_pnl_usd = sum(pnls)
    gross_loss = -sum(losses)
    summary.profit_factor = sum(wins) / gross_loss if gross_loss > 0 else None
    peak = running = drawdown = 0.0
    for pnl in pnls:
        running += pnl
        peak = max(peak, running)
        drawdown = max(drawdown, peak - running)
    summary.max_drawdown_usd = drawdown
    for r in ordered:
        summary.exit_reasons[r.exit_reason] = (
            summary.exit_reasons.get(r.exit_reason, 0) + 1
        )
    return summary


def time_split(
    decisions: Sequence[TrackedDecision], train_fraction: float
) -> tuple[list[TrackedDecision], list[TrackedDecision]]:
    ordered = sorted(decisions, key=lambda d: d.decided_at)
    if not ordered:
        return [], []
    cut = int(len(ordered) * min(max(train_fraction, 0.0), 1.0))
    return ordered[:cut], ordered[cut:]


_NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?(?:e[-+]?\d+)?", re.IGNORECASE)


def reason_category(reason: str) -> str:
    """Collapse 'market cap 3.2 SOL below minimum 5' to 'market cap # SOL below
    minimum #' so every rejection of the same kind is grouped together."""
    return " ".join(_NUMBER.sub("#", reason).split())


@dataclass(frozen=True, slots=True)
class HorizonStats:
    horizon_seconds: float
    tokens: int
    median_return_pct: float | None
    share_dead: float | None
    share_doubled_by_then: float | None


def horizon_stats(
    decisions: Sequence[TrackedDecision],
    horizon_seconds: float,
    rules: ExitRules,
) -> HorizonStats:
    returns: list[float] = []
    dead = doubled = counted = 0
    for decision in decisions:
        entry = entry_observation(decision, rules)
        if entry is None:
            continue
        start, entry_obs = entry
        target = entry_obs.observed_at + horizon_seconds * 0.9
        later = [
            o for o in decision.observations[start + 1 :] if o.observed_at >= target
        ]
        if not later:
            continue
        counted += 1
        entry_price = float(entry_obs.price_usd or 0.0)
        window = [
            o
            for o in decision.observations[start + 1 :]
            if o.observed_at <= later[0].observed_at
            and o.usable(rules.min_exit_liquidity_usd)
        ]
        if any(float(o.price_usd or 0) >= 2 * entry_price for o in window):
            doubled += 1
        at_horizon = later[0]
        if not at_horizon.usable(rules.min_exit_liquidity_usd):
            dead += 1
            returns.append(-100.0)
        else:
            returns.append((float(at_horizon.price_usd or 0) / entry_price - 1) * 100)
    if not counted:
        return HorizonStats(horizon_seconds, 0, None, None, None)
    returns.sort()
    mid = len(returns) // 2
    median = (
        returns[mid] if len(returns) % 2 else (returns[mid - 1] + returns[mid]) / 2
    )
    return HorizonStats(
        horizon_seconds, counted, median, dead / counted, doubled / counted
    )
