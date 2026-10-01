"""Price-volume confirmation engine.

Pure functions over a token's recent board samples (price, rolling m5
volume, m5 buy/sell counts, liquidity). It answers two questions for the
decision layer, never for the executor:

1. Does price-volume behaviour confirm an entry here?
2. For a held token, how urgent is the price-volume case for selling?

Volume is confirmation only: no state here buys or sells anything. The
hunter entry check consults `assess()` only when PV_ENABLED is on, and the
exit side is advisory (`exit_advice`) so existing stops, ladders and
emergency rules keep authority.

Data limits, stated once: DEX Screener gives a rolling five-minute volume
and transaction COUNTS per side, not per-side volume, trade sizes or
wallet identities. "Sell volume" is approximated by volume on samples where
price fell; unique buyers/sellers and wallet concentration are unavailable
and are reported as such rather than guessed. Every calculation uses only
samples at or before the assessment time.
"""

from __future__ import annotations

import math
import os
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

# Entry-confirming states; everything else blocks a PV-gated entry.
ENTRY_STATES = frozenset(
    {"BULL_CONFIRMED", "HEALTHY_DIP_CONFIRMED", "ACCUMULATION_BREAKOUT"}
)
# States that veto an entry the strategy already wants ("miss a trade
# rather than chase a pump"). UNKNOWN is added at assessment time when the
# config says missing history should block.
VETO_STATES = frozenset(
    {
        "BEAR_CONFIRMED",
        "BREAKDOWN_RISK",
        "DEAD_CAT_BOUNCE",
        "DISTRIBUTION_CANDIDATE",
        "BULL_EXHAUSTION",
        "VOLUME_SHOCK",
        "BULL_WEAKENING",
    }
)
# States that argue for selling a held position, mildest first.
EXIT_SEVERITY = ("HOLD", "HOLD_CAUTIOUS", "TAKE_PARTIAL", "REDUCE", "EXIT_REVIEW")


@dataclass(frozen=True, slots=True)
class Sample:
    """One board observation. `price` may be any quantity proportional to
    price (market cap works for fixed-supply tokens); only ratios are used."""

    at: float
    price: float
    volume_m5_usd: float
    buys_m5: int
    sells_m5: int
    liquidity_usd: float | None

    @property
    def trades(self) -> int:
        return self.buys_m5 + self.sells_m5

    @property
    def buy_share(self) -> float | None:
        return self.buys_m5 / self.trades if self.trades > 0 else None

    def usable(self) -> bool:
        return (
            math.isfinite(self.price)
            and self.price > 0
            and math.isfinite(self.volume_m5_usd)
            and self.volume_m5_usd >= 0
            and self.buys_m5 >= 0
            and self.sells_m5 >= 0
        )


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


@dataclass(frozen=True, slots=True)
class PriceVolumeConfig:
    """Every threshold is a starting hypothesis; `launch-guard-eval
    pv-backtest` is how to test them. Names follow the PV_* spec."""

    enabled: bool = False
    # "veto": block entries in VETO_STATES (and failed volume quality);
    # "confirm": additionally require an ENTRY_STATES reading confirmed on
    # confirmations_required of the last confirmation_window samples.
    mode: str = "veto"
    block_unknown: bool = True
    # Which entry paths the gate applies to: board decisions ("MOMENTUM
    # BUY", "BUY ZONE", "BUY NOW", "EARLY BUY") and "COPY" for copied
    # wallet buys. Default MOMENTUM BUY: the only signal type where the
    # 2026-09-25..10-01 replay pointed the right way (see docs/evaluation.md).
    signals: frozenset[str] = frozenset({"MOMENTUM BUY"})
    exit_enabled: bool = False
    horizons_seconds: tuple[float, ...] = (60.0, 300.0, 900.0)
    lookback_seconds: float = 1800.0
    min_history_seconds: float = 300.0
    confirmations_required: int = 3
    confirmation_window: int = 5
    # Price move that counts as up/down on the 1-minute horizon; longer
    # horizons scale it by sqrt(h / 60) (1%, ~2.2%, ~3.9% by default).
    price_move_pct: float = 1.0
    volume_rise_ratio: float = 1.15
    volume_fall_ratio: float = 0.85
    rvol_min: float = 1.2
    exhaustion_rvol: float = 3.0
    shock_move_pct: float = 15.0
    buy_ratio_min: float = 0.55
    liquidity_drop_pct: float = 15.0
    pullback_min_pct: float = 4.0
    pullback_max_pct: float = 25.0
    rally_min_pct: float = 8.0
    dip_light_ratio: float = 0.6
    dip_heavy_ratio: float = 1.0
    rebound_min_pct: float = 2.0
    base_max_range_pct: float = 10.0
    distribution_down_share: float = 0.6
    min_trades_m5: int = 10
    min_quality: float = 50.0
    min_score: float = 0.0
    health_weight: float = 0.5
    signal_weight: float = 0.5

    @classmethod
    def from_env(cls) -> PriceVolumeConfig:
        flag = os.getenv("PV_ENABLED", "false").strip().lower()
        horizons = tuple(
            float(part)
            for part in os.getenv("PV_TIMEFRAMES", "60,300,900").split(",")
            if part.strip()
        )
        config = cls(
            enabled=flag in {"1", "true", "yes", "on"},
            mode=os.getenv("PV_MODE", "veto").strip().lower(),
            signals=frozenset(
                part.strip().upper()
                for part in os.getenv("PV_SIGNALS", "MOMENTUM BUY").split(",")
                if part.strip()
            ),
            exit_enabled=os.getenv("PV_EXIT_ENABLED", "false").strip().lower()
            in {"1", "true", "yes", "on"},
            block_unknown=os.getenv("PV_BLOCK_UNKNOWN", "true").strip().lower()
            in {"1", "true", "yes", "on"},
            horizons_seconds=horizons,
            lookback_seconds=_env_float("PV_LOOKBACK", 1800),
            confirmations_required=int(_env_float("PV_CONFIRMATIONS_REQUIRED", 3)),
            confirmation_window=int(_env_float("PV_CONFIRMATION_WINDOW", 5)),
            rvol_min=_env_float("PV_RVOL_MIN", 1.2),
            exhaustion_rvol=_env_float("PV_EXHAUSTION_RVOL", 3.0),
            pullback_max_pct=_env_float("PV_PULLBACK_MAX", 25),
            liquidity_drop_pct=_env_float("PV_LIQUIDITY_DROP_PCT", 15),
            buy_ratio_min=_env_float("PV_BUY_RATIO_MIN", 0.55),
            min_quality=_env_float("PV_MIN_QUALITY", 50),
            min_score=_env_float("PV_MIN_SCORE", 0),
            health_weight=_env_float("PV_HEALTH_WEIGHT", 0.5),
            signal_weight=_env_float("PV_SIGNAL_WEIGHT", 0.5),
        )
        if (
            not config.horizons_seconds
            or any(h <= 0 for h in config.horizons_seconds)
            or not 0 < config.confirmations_required <= config.confirmation_window
            or config.lookback_seconds < max(config.horizons_seconds)
            or not 0 < config.buy_ratio_min < 1
            or config.mode not in {"veto", "confirm"}
        ):
            raise ValueError(
                "invalid price-volume configuration: positive PV_TIMEFRAMES, "
                "PV_LOOKBACK at least the longest timeframe, 0 < "
                "PV_CONFIRMATIONS_REQUIRED <= PV_CONFIRMATION_WINDOW, and "
                "0 < PV_BUY_RATIO_MIN < 1, and PV_MODE veto or confirm are required"
            )
        return config


@dataclass(frozen=True, slots=True)
class HorizonRead:
    seconds: float
    price_return_pct: float
    volume_change: float | None
    liquidity_change_pct: float | None
    bias: str  # BULL, BULL_WEAK, BEAR, BEAR_WEAK, NEUTRAL

    @property
    def label(self) -> str:
        return {
            "BULL": "bullish",
            "BULL_WEAK": "bullish, fading volume",
            "BEAR": "bearish",
            "BEAR_WEAK": "bearish, fading volume",
        }.get(self.bias, "neutral")


@dataclass(slots=True)
class Assessment:
    state: str
    score: float | None = None
    quality: float | None = None
    confirmations: int = 0
    window: int = 0
    rvol: float | None = None
    buy_share: float | None = None
    liquidity_change_pct: float | None = None
    from_peak_pct: float | None = None
    dip_volume_ratio: float | None = None
    rebound_volume_ratio: float | None = None
    horizons: list[HorizonRead] = field(default_factory=list)
    entry_eligible: bool = False  # passes "confirm" mode
    vetoed: bool = False  # fails "veto" mode
    blockers: list[str] = field(default_factory=list)
    exit_action: str = "HOLD"
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "pv_state": self.state,
            "pv_score": None if self.score is None else round(self.score, 1),
            "pv_quality": None if self.quality is None else round(self.quality, 1),
            "pv_confirmations": f"{self.confirmations}/{self.window}",
            "pv_rvol": None if self.rvol is None else round(self.rvol, 3),
            "pv_entry_eligible": self.entry_eligible,
            "pv_vetoed": self.vetoed,
            "pv_blockers": list(self.blockers),
            "pv_exit_action": self.exit_action,
            "pv_reason": self.reason,
        }


def _usable(samples: Sequence[Sample], at: float | None) -> list[Sample]:
    """Usable samples up to `at`, oldest first. A sample identical to the one
    before it is a cached feed response, not a quiet market: it carries no
    new information and would make every horizon look flat, so it is
    dropped."""
    rows = sorted(
        (s for s in samples if s.usable() and (at is None or s.at <= at)),
        key=lambda s: s.at,
    )
    out: list[Sample] = []
    for s in rows:
        if out and (
            s.price,
            s.volume_m5_usd,
            s.buys_m5,
            s.sells_m5,
            s.liquidity_usd,
        ) == (
            out[-1].price,
            out[-1].volume_m5_usd,
            out[-1].buys_m5,
            out[-1].sells_m5,
            out[-1].liquidity_usd,
        ):
            continue
        out.append(s)
    return out


def _sample_at(rows: Sequence[Sample], when: float, tolerance: float) -> Sample | None:
    """Latest sample at or before `when`, if it is not too stale."""
    best = None
    for s in rows:
        if s.at <= when:
            best = s
        else:
            break
    if best is None or when - best.at > tolerance:
        return None
    return best


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old <= 0:
        return None
    return (new / old - 1) * 100


def _horizon(
    rows: Sequence[Sample], seconds: float, cfg: PriceVolumeConfig
) -> HorizonRead | None:
    now = rows[-1]
    then = _sample_at(rows, now.at - seconds, tolerance=max(60.0, seconds / 2))
    if then is None or then is now:
        return None
    ret = (now.price / then.price - 1) * 100
    vol_change = (
        now.volume_m5_usd / then.volume_m5_usd if then.volume_m5_usd > 0 else None
    )
    liq = _pct(now.liquidity_usd, then.liquidity_usd)
    eps = cfg.price_move_pct * math.sqrt(seconds / 60.0)
    rising = vol_change is not None and vol_change >= cfg.volume_rise_ratio
    falling = vol_change is not None and vol_change <= cfg.volume_fall_ratio
    if ret >= eps:
        bias = "BULL" if rising else "BULL_WEAK" if falling else "BULL_WEAK"
    elif ret <= -eps:
        bias = "BEAR" if rising else "BEAR_WEAK" if falling else "BEAR_WEAK"
    else:
        bias = "NEUTRAL"
    return HorizonRead(seconds, ret, vol_change, liq, bias)


def _rvol(rows: Sequence[Sample], cfg: PriceVolumeConfig) -> float | None:
    """Current m5 volume over the median m5 volume of the lookback, leaving
    out the last five minutes (the current window overlaps them)."""
    now = rows[-1]
    base = [
        s.volume_m5_usd
        for s in rows
        if now.at - cfg.lookback_seconds <= s.at <= now.at - 300
    ]
    if len(base) < 3:
        return None
    median = statistics.median(base)
    return now.volume_m5_usd / median if median > 0 else None


def _down_volume_share(rows: Sequence[Sample]) -> float | None:
    """Share of volume printed on samples where price fell. A proxy for sell
    volume: the feed has no per-side volume."""
    up = down = 0.0
    for prev, cur in zip(rows, rows[1:], strict=False):
        if cur.price > prev.price:
            up += cur.volume_m5_usd
        elif cur.price < prev.price:
            down += cur.volume_m5_usd
    total = up + down
    return down / total if total > 0 else None


def volume_quality(
    rows: Sequence[Sample], rvol: float | None, cfg: PriceVolumeConfig
) -> tuple[float, list[str]]:
    """0-100. Penalises activity that looks unlike real demand. Unique
    wallets and size concentration are not in the feed, so not scored."""
    now = rows[-1]
    score, flags = 100.0, []
    if now.trades < cfg.min_trades_m5:
        score -= 40
        flags.append(f"only {now.trades} trades in 5m")
    if now.trades > 0 and now.liquidity_usd:
        avg = now.volume_m5_usd / now.trades
        if avg / now.liquidity_usd > 0.02:
            score -= 25
            flags.append("average trade is over 2% of the pool")
    if now.liquidity_usd and now.volume_m5_usd / now.liquidity_usd > 3:
        score -= 20
        flags.append("5m volume over 3x liquidity (churn)")
    if rvol is not None and rvol >= cfg.exhaustion_rvol and len(rows) >= 2:
        prev = _sample_at(rows, now.at - 300, tolerance=180)
        move = abs(_pct(now.price, prev.price) or 0.0) if prev else 0.0
        if move < 2:
            score -= 30
            flags.append("volume spike with almost no price response")
    return max(0.0, score), flags


@dataclass(slots=True)
class _Swing:
    from_peak_pct: float
    depth_pct: float
    rally_pct: float
    dip_ratio: float | None
    rebound_pct: float
    rebound_ratio: float | None
    decline_heavy: bool


def _swing(rows: Sequence[Sample], cfg: PriceVolumeConfig) -> _Swing | None:
    """The latest rally into a lookback peak and what came after it."""
    window = [s for s in rows if s.at >= rows[-1].at - cfg.lookback_seconds]
    if len(window) < 4:
        return None
    peak_i = max(range(len(window)), key=lambda i: window[i].price)
    if peak_i == len(window) - 1:
        return None
    before = window[: peak_i + 1]
    low_i = min(range(len(before)), key=lambda i: before[i].price)
    rally = before[low_i:]
    rally_pct = (window[peak_i].price / before[low_i].price - 1) * 100
    after = window[peak_i + 1 :]
    peak = window[peak_i].price
    now = window[-1]
    from_peak = (peak - now.price) / peak * 100
    rally_vol = max(s.volume_m5_usd for s in rally)
    dip_vol = max(s.volume_m5_usd for s in after)
    low_j = min(range(len(after)), key=lambda j: after[j].price)
    decline = after[: low_j + 1]
    rebound = after[low_j + 1 :]
    decline_vol = max(s.volume_m5_usd for s in decline)
    rebound_pct = (now.price / after[low_j].price - 1) * 100
    rebound_ratio = (
        max(s.volume_m5_usd for s in rebound) / after[low_j].volume_m5_usd
        if rebound and after[low_j].volume_m5_usd > 0
        else None
    )
    return _Swing(
        from_peak_pct=from_peak,
        depth_pct=(peak - after[low_j].price) / peak * 100,
        rally_pct=rally_pct,
        dip_ratio=dip_vol / rally_vol if rally_vol > 0 else None,
        rebound_pct=rebound_pct,
        rebound_ratio=rebound_ratio,
        decline_heavy=rally_vol > 0 and decline_vol / rally_vol >= cfg.dip_heavy_ratio,
    )


def _classify(rows: Sequence[Sample], cfg: PriceVolumeConfig) -> Assessment:
    """State at the last sample of `rows` (no persistence, no verdict)."""
    now = rows[-1]
    a = Assessment(state="UNKNOWN")
    if now.at - rows[0].at < cfg.min_history_seconds:
        a.reason = "not enough history yet"
        return a
    a.horizons = [
        h for h in (_horizon(rows, s, cfg) for s in cfg.horizons_seconds) if h
    ]
    if not a.horizons:
        a.reason = "no comparable earlier samples"
        return a
    a.rvol = _rvol(rows, cfg)
    a.buy_share = now.buy_share
    structure = a.horizons[-1]
    short = next((h for h in a.horizons if h.seconds >= 300), a.horizons[-1])
    start = _sample_at(rows, now.at - cfg.lookback_seconds, cfg.lookback_seconds)
    a.liquidity_change_pct = _pct(now.liquidity_usd, (start or rows[0]).liquidity_usd)
    buy = a.buy_share if a.buy_share is not None else 0.5
    liq_drain = (
        a.liquidity_change_pct is not None
        and a.liquidity_change_pct <= -cfg.liquidity_drop_pct
    )
    swing = _swing(rows, cfg)
    if swing:
        a.from_peak_pct = swing.from_peak_pct
        a.dip_volume_ratio = swing.dip_ratio
        a.rebound_volume_ratio = swing.rebound_ratio

    if liq_drain:
        a.state = "BREAKDOWN_RISK"
        a.reason = (
            f"liquidity down {-(a.liquidity_change_pct or 0):.0f}% over the lookback"
        )
        return a

    # Volume climax: extreme relative volume on an outsized move.
    if (
        a.rvol is not None
        and a.rvol >= cfg.exhaustion_rvol
        and abs(short.price_return_pct) >= cfg.shock_move_pct
    ):
        a.state = "VOLUME_SHOCK"
        a.reason = (
            f"{a.rvol:.1f}x normal volume on a {short.price_return_pct:+.0f}% move; "
            "wait for the next samples"
        )
        return a
    # A pullback episode: a real rally, then a dip of at least
    # pullback_min_pct below its peak (measured at the dip's low, so a dip
    # that is already rebounding still counts), price not back at the high.
    in_pullback = (
        swing is not None
        and swing.rally_pct >= cfg.rally_min_pct
        and swing.depth_pct >= cfg.pullback_min_pct
        and swing.from_peak_pct > 0
    )
    if in_pullback and swing is not None:
        heavy = swing.dip_ratio is not None and swing.dip_ratio >= cfg.dip_heavy_ratio
        rebounding = swing.rebound_pct >= cfg.rebound_min_pct
        if (
            rebounding
            and swing.decline_heavy
            and (swing.rebound_ratio is None or swing.rebound_ratio < 1.0)
        ):
            a.state = "DEAD_CAT_BOUNCE"
            a.reason = "heavy-volume selloff followed by a bounce on no new volume"
            return a
        if heavy and short.bias in {"BEAR", "BEAR_WEAK"} and not rebounding:
            a.state = "BREAKDOWN_RISK"
            a.reason = (
                f"dip traded {swing.dip_ratio:.2f}x the rally's volume and price "
                "is still falling"
            )
            return a
        if swing.depth_pct > cfg.pullback_max_pct:
            a.state = "BREAKDOWN_RISK"
            a.reason = f"{swing.depth_pct:.0f}% below the peak: deeper than a dip"
            return a
        if not heavy:
            reexpanding = (
                swing.rebound_ratio is not None
                and swing.rebound_ratio >= cfg.volume_rise_ratio
            )
            if rebounding and reexpanding and buy >= cfg.buy_ratio_min:
                a.state = "HEALTHY_DIP_CONFIRMED"
                a.reason = (
                    "selling volume contracted on the dip and buyers returned "
                    "on rising volume"
                )
            else:
                a.state = "HEALTHY_DIP_CANDIDATE"
                a.reason = "light-volume dip; waiting for buyers and volume to return"
            return a
        a.state = "PULLBACK"
        a.reason = "pullback on heavy volume; not yet a confirmed breakdown"
        return a

    # Exhaustion / climax only outside a measured pullback: after a rally,
    # a dip is the pullback classifier's job, not a climax read.
    recent_shock = _recent_shock(rows, cfg)
    if recent_shock == "UP" and (buy < 0.5 or short.bias in {"BEAR", "BEAR_WEAK"}):
        a.state = "BULL_EXHAUSTION"
        a.reason = "after a volume climax up, buyers are fading and price is slipping"
        return a
    if (
        recent_shock == "DOWN"
        and buy >= cfg.buy_ratio_min
        and short.bias in {"NEUTRAL", "BULL", "BULL_WEAK"}
    ):
        a.state = "SELLING_CLIMAX_CANDIDATE"
        a.reason = (
            "after a volume climax down, price has stabilised and buyers returned"
        )
        return a

    window = [s for s in rows if s.at >= now.at - structure.seconds]
    down_share = _down_volume_share(window)
    peak = max(s.price for s in rows if s.at >= now.at - cfg.lookback_seconds)
    near_high = now.price >= peak * 0.95
    if (
        near_high
        and down_share is not None
        and down_share >= cfg.distribution_down_share
        and (buy < 0.5 or short.bias in {"BULL_WEAK", "NEUTRAL", "BEAR_WEAK"})
    ):
        a.state = "DISTRIBUTION_CANDIDATE"
        a.reason = (
            f"near the high but {down_share:.0%} of recent volume printed on down-ticks"
        )
        return a

    base = [s for s in rows if now.at - structure.seconds <= s.at < now.at - 60]
    if len(base) >= 4:
        hi, lo = max(s.price for s in base), min(s.price for s in base)
        tight = (hi / lo - 1) * 100 <= cfg.base_max_range_pct
        base_down = _down_volume_share(base)
        if (
            tight
            and now.price > hi * 1.01
            and (a.rvol or 0) >= cfg.rvol_min
            and buy >= cfg.buy_ratio_min
        ):
            a.state = "ACCUMULATION_BREAKOUT"
            a.reason = (
                f"broke a {((hi / lo - 1) * 100):.1f}% base on "
                f"{a.rvol:.1f}x relative volume with buyers in control"
            )
            return a
        if tight and base_down is not None and base_down <= 0.45 and buy >= 0.5:
            a.state = "ACCUMULATION_CANDIDATE"
            a.reason = "tight base, selling volume fading, buyers improving"
            return a

    if (
        short.bias == "BULL"
        and buy >= cfg.buy_ratio_min
        and structure.bias not in {"BEAR", "BEAR_WEAK"}
    ):
        a.state = "BULL_CONFIRMED"
        a.reason = "price and volume rising together with buyers in control"
    elif short.bias in {"BULL", "BULL_WEAK"}:
        a.state = "BULL_WEAKENING"
        a.reason = "price rising but participation is not confirming"
    elif short.bias == "BEAR":
        a.state = "BEAR_CONFIRMED"
        a.reason = "price falling on rising volume"
    elif short.bias == "BEAR_WEAK":
        a.state = "BEAR_WEAKENING"
        a.reason = "price falling but selling is fading; watch for stabilisation"
    else:
        a.state = "WATCH"
        a.reason = "no clear price-volume direction"
    return a


def _recent_shock(rows: Sequence[Sample], cfg: PriceVolumeConfig) -> str | None:
    """Direction of a volume climax 1-5 minutes ago, if any."""
    now = rows[-1]
    for s in reversed(rows[:-1]):
        age = now.at - s.at
        if age < 60:
            continue
        if age > 300:
            break
        prefix = [r for r in rows if r.at <= s.at]
        if len(prefix) < 3:
            break
        rv = _rvol(prefix, cfg)
        prev = _sample_at(prefix, s.at - 300, tolerance=180)
        move = _pct(s.price, prev.price) if prev else None
        if (
            rv is not None
            and move is not None
            and rv >= cfg.exhaustion_rvol
            and abs(move) >= cfg.shock_move_pct
        ):
            return "UP" if move > 0 else "DOWN"
    return None


def _score(a: Assessment, cfg: PriceVolumeConfig) -> float:
    weights = {
        "BULL": 1.0,
        "BULL_WEAK": 0.5,
        "NEUTRAL": 0.25,
        "BEAR_WEAK": 0.1,
        "BEAR": 0.0,
    }
    trend = 25 * sum(weights[h.bias] for h in a.horizons) / max(1, len(a.horizons))
    if a.state in {"HEALTHY_DIP_CANDIDATE", "HEALTHY_DIP_CONFIRMED"}:
        # A dip is supposed to be quiet: score contraction, not expansion.
        ratio = a.dip_volume_ratio
        rvol_part = 25.0 if ratio is not None and ratio <= cfg.dip_light_ratio else 15.0
    elif a.rvol is None:
        rvol_part = 8.0
    elif a.rvol >= cfg.exhaustion_rvol:
        rvol_part = 10.0
    elif a.rvol >= cfg.rvol_min:
        rvol_part = 25.0
    elif a.rvol >= 0.8:
        rvol_part = 12.0
    else:
        rvol_part = 5.0
    buy = a.buy_share if a.buy_share is not None else 0.5
    buyers = 20 * min(1.0, max(0.0, (buy - 0.4) / 0.3))
    liq = a.liquidity_change_pct
    liquidity = 8.0 if liq is None else 15.0 if liq >= 0 else max(0.0, 15 + liq)
    persistence = 15 * a.confirmations / max(1, a.window)
    return trend + rvol_part + buyers + liquidity + persistence


def _exit_action(a: Assessment, cfg: PriceVolumeConfig) -> str:
    buy = a.buy_share if a.buy_share is not None else 0.5
    if a.state == "BREAKDOWN_RISK":
        drained = (a.liquidity_change_pct or 0) <= -cfg.liquidity_drop_pct
        return "EXIT_REVIEW" if drained or buy < 0.35 else "REDUCE"
    if a.state in {"BEAR_CONFIRMED", "DEAD_CAT_BOUNCE"}:
        return "REDUCE"
    if a.state in {"DISTRIBUTION_CANDIDATE", "BULL_EXHAUSTION"}:
        return "TAKE_PARTIAL"
    if a.state == "BULL_WEAKENING":
        weak = sum(
            (
                buy < 0.5,
                (a.liquidity_change_pct or 0) < -5,
                any(h.bias in {"BEAR", "BEAR_WEAK"} for h in a.horizons),
            )
        )
        return "TAKE_PARTIAL" if weak >= 2 else "HOLD_CAUTIOUS"
    if a.state in {"PULLBACK", "VOLUME_SHOCK"}:
        return "HOLD_CAUTIOUS"
    return "HOLD"


def entry_block_reason(
    path: str, reading: Mapping[str, object], cfg: PriceVolumeConfig
) -> str | None:
    """The shared entry gate for every buy path (hunter, auto-buy, copy).

    `reading` holds the board's pv_* fields (a snapshot row, or a
    candidate's attributes). None means allowed: the gate is off, does not
    cover this path, or the reading passes the configured mode. Missing
    fields count as UNKNOWN, never as favourable."""
    if not cfg.enabled or path.upper() not in cfg.signals:
        return None
    state = str(reading.get("pv_state") or "UNKNOWN")
    if state == "UNKNOWN":
        return "price-volume history unavailable" if cfg.block_unknown else None
    if cfg.mode == "confirm":
        if reading.get("pv_entry_eligible") is True:
            return None
        return f"price-volume does not confirm ({state})"
    if reading.get("pv_vetoed") is False:
        return None
    return f"price-volume veto ({state})"


def allows_entry(a: Assessment, cfg: PriceVolumeConfig) -> bool:
    """The one place the configured mode turns an assessment into a gate."""
    return a.entry_eligible if cfg.mode == "confirm" else not a.vetoed


def assess(
    samples: Sequence[Sample],
    cfg: PriceVolumeConfig,
    *,
    at: float | None = None,
    health_score: float | None = None,
) -> Assessment:
    """Full assessment at `at` (default: the latest sample), using only
    samples at or before it."""
    rows = _usable(samples, at)
    if rows:
        # Nothing reads further back than the lookback; bound the work.
        horizon = cfg.lookback_seconds + max(cfg.horizons_seconds) + 60
        rows = [s for s in rows if s.at >= rows[-1].at - horizon]
    if not rows:
        return Assessment(
            state="UNKNOWN",
            reason="no usable samples",
            blockers=["no price-volume history"],
            vetoed=cfg.block_unknown,
        )
    a = _classify(rows, cfg)
    window = rows[-cfg.confirmation_window :]
    a.window = cfg.confirmation_window
    a.confirmations = sum(
        _classify(rows[: rows.index(s) + 1], cfg).state in ENTRY_STATES
        for s in window[:-1]
    ) + (a.state in ENTRY_STATES)
    if a.state == "UNKNOWN":
        a.blockers = ["not enough price-volume history"]
        a.exit_action = "HOLD"
        a.vetoed = cfg.block_unknown
        return a
    a.quality, flags = volume_quality(rows, a.rvol, cfg)
    a.score = _score(a, cfg)
    blockers = []
    if a.state not in ENTRY_STATES:
        blockers.append(f"state {a.state} does not confirm an entry")
    if a.confirmations < cfg.confirmations_required:
        blockers.append(
            f"only {a.confirmations}/{cfg.confirmation_window} recent samples confirm"
        )
    if a.quality < cfg.min_quality:
        blockers.append("volume quality: " + "; ".join(flags))
    if a.horizons and a.horizons[-1].bias == "BEAR":
        blockers.append("longest timeframe is bearish")
    if cfg.min_score and a.score < cfg.min_score:
        blockers.append(f"price-volume score {a.score:.0f} below {cfg.min_score:.0f}")
    a.blockers = blockers
    a.entry_eligible = not blockers
    a.vetoed = a.state in VETO_STATES or a.quality < cfg.min_quality
    a.exit_action = _exit_action(a, cfg)
    if health_score is not None:
        a.reason += (
            f" (combined entry score "
            f"{combined_entry_score(health_score, a.score, cfg):.0f})"
        )
    return a


def combined_entry_score(
    health: float, pv: float | None, cfg: PriceVolumeConfig
) -> float:
    """Health and price-volume combined only at the decision layer."""
    if pv is None:
        return health
    total = cfg.health_weight + cfg.signal_weight
    return (
        (cfg.health_weight * health + cfg.signal_weight * pv) / total
        if total
        else health
    )


def explain(a: Assessment, symbol: str = "") -> str:
    """Log/terminal block describing why the state was reached."""
    lines = [f"TOKEN: {symbol}" if symbol else "TOKEN: ?", f"STATE: {a.state}"]
    if a.from_peak_pct is not None:
        lines.append(f"PRICE: {-a.from_peak_pct:+.1f}% from local peak")
    if a.dip_volume_ratio is not None:
        lines.append(f"DIP VOLUME: {a.dip_volume_ratio:.2f}x rally volume")
    if a.rvol is not None:
        lines.append(f"RVOL: {a.rvol:.2f}")
    if a.buy_share is not None:
        lines.append(f"BUY RATIO: {a.buy_share:.0%} of 5m trades")
    if a.liquidity_change_pct is not None:
        lines.append(f"LIQUIDITY: {a.liquidity_change_pct:+.1f}%")
    if a.horizons:
        lines.append(
            "TIMEFRAMES: "
            + ", ".join(f"{h.seconds / 60:g}m {h.label}" for h in a.horizons)
        )
    lines.append(f"CONFIRMATIONS: {a.confirmations} / {a.window}")
    if a.score is not None:
        lines.append(f"PRICE-VOLUME SCORE: {a.score:.0f}")
    if a.quality is not None:
        lines.append(f"VOLUME QUALITY: {a.quality:.0f}")
    lines.append(
        "ENTRY (confirm mode): " + ("ELIGIBLE" if a.entry_eligible else "BLOCKED")
    )
    lines.append("ENTRY (veto mode): " + ("VETOED" if a.vetoed else "ALLOWED"))
    lines.append(f"IF HELD: {a.exit_action}")
    lines.append(f"REASON: {a.reason}")
    if a.blockers:
        lines.append("BLOCKERS: " + "; ".join(a.blockers))
    return "\n".join(lines)
