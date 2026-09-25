#!/usr/bin/env python3
"""
coin_tracker.py - adaptive tracker / position manager for Solana DEX tokens.

Polls the public DexScreener API, keeps a rolling history per token, and scores
each token's "health" (0-100) from liquidity, liquidity/mcap, volume turnover,
buy pressure, age and liquidity trend. The score sets the trading rules:

  * Healthier coins get wider, volatility-scaled trailing stops and higher
    take-profit targets. Weaker coins get tighter rules.
  * Normal dips and quiet stretches do NOT trigger a sell. A stop exit needs
    several readings in a row below the stop, and "no new high" only matters
    when the coin is also showing weakness.
  * Dip-buys happen only on healthy coins whose liquidity is holding up, and
    only for normal-sized dips that have stopped falling (not crashes).
  * Liquidity pulls, a collapse in liquidity, or heavy sell-off skip all the
    patience rules and exit immediately.

Runs in PAPER mode by default (simulated fills with estimated slippage).

Shares its DexScreener access with the rest of solana_launch_guard instead
of polling independently: quotes come from DexScreenerOracle.quote_many(),
the same batched-request client (tokens/v1, up to 30 mints per HTTP call,
429-retry with backoff) the live trial and portfolio monitor already use.
A small watchlist like this one's costs a single request per poll cycle
either way, but going through the shared oracle means one fewer raw,
unregulated poller hitting DexScreener's rate limit from this machine, and
any future rate-limit tuning done for the shared oracle benefits this too
without a separate fix here.
"""

import asyncio
import json
import logging
import math
import os
import re
import statistics
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

from solana_launch_guard.market import DexScreenerOracle, MarketQuote

# 2026-09-24 20:32 local: the $50-position/dip-buy era ended here (position
# size dropped to $5, dip-buys turned off - see CONFIG below). Trades from
# before this still count toward total_pnl, but status/monitoring reports
# before/after separately so those old losses don't dominate the headline
# number for weeks.
PNL_SPLIT_AT = 1790307124.0  # 2026-09-24 20:32:04 local

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
CONFIG = {
    # Always watched, whatever the board says.
    "pinned": [
        "DFQHUegJWE29Xu3BUxPezqi77uyHURRyxJpdtyvLpump",
        "4WECKfvfgEiojyZJq5Xm12Hvk76rhM4VHQAZGaELpump",
    ],
    # Dynamic watchlist: follow the main bot's board (read-only) so this
    # tracker paper-trades the same pool the live strategy trades. Filters
    # default to the live strategy profile (ENTRY_MIN_TOKEN_AGE_DAYS and
    # AUTO_BUY_DISCOVERY_MIN_LIQUIDITY_USD). Tokens with an open paper
    # position are never dropped, even after they leave the board.
    "dynamic_watchlist": True,
    "board_snapshot_path": os.getenv(
        "RECOMMENDATION_SNAPSHOT_PATH", "launch_guard_recommendations.json"
    ),
    "board_refresh_seconds": 300,
    "board_max_tokens": 20,
    "board_min_age_days": float(os.getenv("ENTRY_MIN_TOKEN_AGE_DAYS") or 3),
    "board_min_liquidity_usd": float(
        os.getenv("AUTO_BUY_DISCOVERY_MIN_LIQUIDITY_USD") or 50_000
    ),
    "board_max_snapshot_age_seconds": 900,  # older file = bot not running
    "poll_seconds": 30,
    "warmup_minutes": 15,             # observe this long before any trade
    "position_usd": 5.0,              # first buy; matches live orders so
                                      # simulated price impact is comparable
    # Adding to a falling position is averaging down - the same risk as the
    # live recovery re-buy that is switched off. Off by default; turn on to
    # compare results with and without it.
    "dip_buys_enabled": False,
    "dip_add_fraction": 0.5,          # each dip-buy = this x first buy
    "max_total_usd_per_token": 150.0,
    "entry_min_health": 60,           # 0-100
    "reentry_cooldown_min": 30,       # wait after an exit before re-entering
    # A token that loses this many times within the window (e.g. Fartcoin's
    # liquidity-danger flag, three losses in a row before this fix, only a
    # 30-minute cooldown apart each time) stops being re-entered for the
    # rest of the window instead of paying to re-learn the same lesson.
    "repeat_loss_block_count": 2,
    "repeat_loss_window_hours": 24.0,
    "auto_enter": True,
    "history_hours": 6,
    "state_file": "tracker_state.json",
    "log_file": "tracker.log",
}

log = logging.getLogger("tracker")


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
@dataclass
class Snapshot:
    ts: float
    price: float
    liquidity: float
    mcap: float
    vol_m5: float
    vol_h1: float
    vol_h24: float
    buys_m5: int
    sells_m5: int
    buys_h1: int
    sells_h1: int
    pair_created_ms: float


def snapshot_from_quote(quote: MarketQuote | None) -> "Snapshot | None":
    """Adapts the shared oracle's MarketQuote into this module's own
    Snapshot shape, unchanged from the original standalone version, so
    every downstream scoring/rule function below needed no changes."""
    if quote is None or not quote.price_usd or quote.price_usd <= 0:
        return None
    return Snapshot(
        ts=time.time(),
        price=quote.price_usd,
        liquidity=quote.liquidity_usd or 0.0,
        mcap=quote.market_cap_usd or 0.0,
        vol_m5=quote.volume_m5_usd,
        vol_h1=quote.volume_h1_usd,
        vol_h24=quote.volume_h24_usd,
        buys_m5=quote.buys_m5,
        sells_m5=quote.sells_m5,
        buys_h1=quote.buys_h1,
        sells_h1=quote.sells_h1,
        pair_created_ms=float(quote.pair_created_at_ms or 0),
    )


class TokenHistory:
    def __init__(self, maxlen):
        self.snaps = deque(maxlen=maxlen)

    def add(self, s):
        self.snaps.append(s)

    @property
    def last(self):
        return self.snaps[-1]

    def minutes_observed(self):
        if len(self.snaps) < 2:
            return 0.0
        return (self.snaps[-1].ts - self.snaps[0].ts) / 60

    def window(self, minutes):
        cutoff = self.last.ts - minutes * 60
        return [s for s in self.snaps if s.ts >= cutoff]

    def pct_change(self, attr, minutes):
        old = getattr(self.window(minutes)[0], attr)
        new = getattr(self.last, attr)
        return (new - old) / old if old > 0 else 0.0

    def drop_from_peak(self, attr, minutes):
        peak = max(getattr(s, attr) for s in self.window(minutes))
        return getattr(self.last, attr) / peak - 1 if peak > 0 else 0.0

    def avg(self, attr, minutes):
        return statistics.fmean(getattr(s, attr) for s in self.window(minutes))

    def move_unit(self):
        """Typical 5-minute price move (as a fraction), from the last hour."""
        w = [s for s in self.window(60) if s.price > 0]
        rets = [math.log(b.price / a.price) for a, b in zip(w, w[1:])]
        if len(rets) < 5:
            return 0.05
        sec_per_poll = (w[-1].ts - w[0].ts) / (len(w) - 1)
        polls_per_5m = max(300 / max(sec_per_poll, 1), 1)
        return max(statistics.pstdev(rets) * math.sqrt(polls_per_5m), 0.005)


# --------------------------------------------------------------------------
# Scoring and rules
# --------------------------------------------------------------------------
def health_score(h):
    s = h.last
    age_h = (s.ts * 1000 - s.pair_created_ms) / 3.6e6 if s.pair_created_ms else 0
    liq_ratio = s.liquidity / s.mcap if s.mcap else 0
    turnover = s.vol_h24 / s.liquidity if s.liquidity else 0
    total_h1 = s.buys_h1 + s.sells_h1
    buy_pressure = s.buys_h1 / total_h1 if total_h1 else 0.5
    liq_trend = h.pct_change("liquidity", 60)
    vol_trend = s.vol_h1 / (s.vol_h24 / 24) if s.vol_h24 else 0  # 1.0 = normal hour

    if turnover < 2:
        turnover_score = turnover / 2                   # too quiet
    else:
        turnover_score = clamp(1 - (turnover - 15) / 25)  # >15x starts to look like churn

    parts = {
        "liquidity": (0.25, clamp((math.log10(max(s.liquidity, 1)) - 4.3) / 1.7)),  # $20k->0, $1M->1
        "liq_ratio": (0.15, clamp((liq_ratio - 0.02) / 0.13)),                       # 2%->0, 15%->1
        "turnover": (0.15, turnover_score),
        "buy_pressure": (0.15, clamp((buy_pressure - 0.40) / 0.20)),                 # 40%->0, 60%->1
        "age": (0.15, clamp(age_h / 72)),                                            # 3 days = full track record
        "liq_trend": (0.15, clamp(1 + liq_trend / 0.20)),                            # -20%/h->0, flat->1
    }
    score = 100 * sum(w * v for w, v in parts.values())
    info = {
        "age_h": age_h, "liq_ratio": liq_ratio, "turnover": turnover,
        "buy_pressure": buy_pressure, "liq_trend_1h": liq_trend, "vol_trend": vol_trend,
    }
    return score, info


@dataclass
class Profile:
    stop_k: float        # trailing stop distance, in 5-minute move units
    min_stop: float
    max_stop: float
    confirm: int         # consecutive readings below stop before selling
    tp_levels: list      # [(gain, fraction of current holding to sell)]
    stall_minutes: float
    max_adds: int


def profile_for(score):
    h = score / 100
    return Profile(
        stop_k=2.5 + 3.5 * h,
        min_stop=0.08 + 0.07 * h,
        max_stop=0.25 + 0.15 * h,
        confirm=2 + round(4 * h),
        tp_levels=[(0.30 * (1 + 2 * h), 0.35), (0.80 * (1 + 2 * h), 0.50)],
        stall_minutes=15 + 75 * h,
        max_adds=0 if score < 55 else (2 if score < 75 else 3),
    )


def danger_signals(h):
    """Anything here exits immediately, skipping confirmation."""
    s, out = h.last, []
    liq15 = h.drop_from_peak("liquidity", 15)
    liq60 = h.drop_from_peak("liquidity", 60)
    if liq15 <= -0.25:
        out.append(f"liquidity -{abs(liq15):.0%} in 15m")
    if liq60 <= -0.40:
        out.append(f"liquidity -{abs(liq60):.0%} in 1h")
    if s.liquidity < 10_000:
        out.append(f"liquidity only ${s.liquidity:,.0f}")
    if (s.sells_m5 >= 20 and s.sells_m5 >= 3 * max(s.buys_m5, 1)
            and h.pct_change("price", 5) <= -0.15):
        out.append("heavy sell-off")
    return out


def warning_signals(h, info):
    """Signs of weakening. These tighten the rules but don't force a sell."""
    out = []
    if info["vol_trend"] < 0.35:
        out.append("volume fading")
    if info["buy_pressure"] < 0.45:
        out.append("sellers dominating")
    if h.drop_from_peak("liquidity", 60) <= -0.12:
        out.append("liquidity slipping")
    return out


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------
class PaperExecutor:
    """Simulated fills: constant-product price impact plus ~1% fees."""

    def buy(self, token, usd, snap):
        impact = usd / (snap.liquidity / 2 + usd)
        return usd / (snap.price * (1 + impact) * 1.01)  # tokens received

    def sell(self, token, qty, snap):
        usd = qty * snap.price
        impact = usd / (snap.liquidity / 2 + usd)
        return usd * (1 - impact) * 0.99                 # USD received


class LiveExecutor:
    """Placeholder. Wire up real swaps (e.g. via Jupiter) here with your own
    wallet handling, and only after paper trading looks right."""

    def buy(self, token, usd, snap):
        raise NotImplementedError

    def sell(self, token, qty, snap):
        raise NotImplementedError


@dataclass
class Position:
    qty: float
    cost_usd: float
    peak: float
    opened: float
    last_high: float
    tps_hit: int = 0
    adds: int = 0
    last_add: float = 0.0
    below_count: int = 0
    realized_usd: float = 0.0

    @property
    def avg_entry(self):
        return self.cost_usd / self.qty if self.qty else 0.0


# --------------------------------------------------------------------------
# Dynamic watchlist
# --------------------------------------------------------------------------
def read_board(path, max_age_seconds, now=None):
    """Board rows from the main bot's snapshot, or None when the file is
    missing, unreadable or stale (so the caller keeps its current list
    instead of dropping everything because the bot restarted)."""
    try:
        snapshot = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    now = time.time() if now is None else now
    generated_at = float(snapshot.get("generated_at") or 0.0)
    if now - generated_at > max_age_seconds:
        return None
    rows = snapshot.get("candidates")
    return rows if isinstance(rows, list) else None


def select_watchlist(board_rows, pinned, held, cfg, now=None):
    """Pinned mints, then open positions, then the best board tokens that
    pass the live strategy's age and liquidity floors, up to the cap."""
    now = time.time() if now is None else now
    chosen = list(dict.fromkeys([*pinned, *held]))
    eligible = []
    for row in board_rows or []:
        if not isinstance(row, dict) or row.get("chain", "solana") != "solana":
            continue
        mint = row.get("mint")
        created = row.get("pair_created_at_ms")
        if not mint or mint in chosen or not created:
            continue
        age_days = (now * 1000 - float(created)) / 86_400_000
        if age_days < cfg["board_min_age_days"]:
            continue
        if float(row.get("liquidity_usd") or 0) < cfg["board_min_liquidity_usd"]:
            continue
        eligible.append((float(row.get("signal_score") or 0), mint))
    eligible.sort(reverse=True)
    room = max(0, cfg["board_max_tokens"] - len(chosen))
    return chosen + [mint for _, mint in eligible[:room]]


# --------------------------------------------------------------------------
# Bot
# --------------------------------------------------------------------------
class Bot:
    def __init__(self, cfg, executor):
        self.cfg, self.ex = cfg, executor
        self.oracle = DexScreenerOracle()
        self.maxlen = int(cfg["history_hours"] * 3600 / cfg["poll_seconds"])
        self.watchlist = list(cfg["pinned"])
        self.hist = {t: TokenHistory(self.maxlen) for t in self.watchlist}
        self.positions, self.cooldown, self.total_pnl = {}, {}, 0.0
        self.loss_history: dict[str, list[float]] = {}
        self.pnl_before_split, self.pnl_since_split = 0.0, 0.0
        self.last_board_refresh = 0.0
        self._load()
        for t in self.watchlist:
            self.hist.setdefault(t, TokenHistory(self.maxlen))

    # ---- persistence -------------------------------------------------------
    def _load(self):
        p = Path(self.cfg["state_file"])
        if not p.exists():
            return
        st = json.loads(p.read_text())
        self.positions = {t: Position(**d) for t, d in st.get("positions", {}).items()}
        self.cooldown = st.get("cooldown", {})
        self.total_pnl = st.get("total_pnl", 0.0)
        self.loss_history = {t: list(v) for t, v in st.get("loss_history", {}).items()}
        if "pnl_before_split" in st and "pnl_since_split" in st:
            self.pnl_before_split = st["pnl_before_split"]
            self.pnl_since_split = st["pnl_since_split"]
        else:
            self.pnl_before_split, self.pnl_since_split = self._backfill_pnl_split()
        saved = st.get("watchlist") or []
        self.watchlist = list(dict.fromkeys([*self.watchlist, *saved, *self.positions]))
        for t, snaps in st.get("history", {}).items():
            if t in self.watchlist:
                h = self.hist.setdefault(t, TokenHistory(self.maxlen))
                for s in snaps:
                    h.add(Snapshot(**s))
        log.info("Loaded state: %d open positions", len(self.positions))

    def _backfill_pnl_split(self):
        """One-time reconstruction from tracker.log's own CLOSED lines, for
        a state file saved before the before/after split existed. Runs once;
        afterward pnl_before_split/pnl_since_split are persisted and updated
        incrementally in _close() instead."""
        before = after = 0.0
        log_path = Path(self.cfg["log_file"])
        if not log_path.exists():
            return before, after
        pattern = re.compile(
            r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \S+: CLOSED\. "
            r"Trade P&L \$([+-]?\d+\.\d+) \|"
        )
        for line in log_path.read_text(errors="ignore").splitlines():
            m = pattern.match(line)
            if not m:
                continue
            ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
            pnl = float(m.group(2))
            if ts >= PNL_SPLIT_AT:
                after += pnl
            else:
                before += pnl
        return before, after

    def save(self):
        st = {
            "watchlist": self.watchlist,
            "positions": {t: asdict(p) for t, p in self.positions.items()},
            "cooldown": self.cooldown,
            "total_pnl": self.total_pnl,
            "loss_history": self.loss_history,
            "pnl_before_split": self.pnl_before_split,
            "pnl_since_split": self.pnl_since_split,
            "history": {t: [asdict(s) for s in h.snaps][-240:] for t, h in self.hist.items()},
        }
        tmp = Path(self.cfg["state_file"] + ".tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(self.cfg["state_file"])

    # ---- watchlist ---------------------------------------------------------
    def refresh_watchlist(self, now=None):
        now = time.time() if now is None else now
        if not self.cfg["dynamic_watchlist"]:
            return
        if now - self.last_board_refresh < self.cfg["board_refresh_seconds"]:
            return
        self.last_board_refresh = now
        rows = read_board(
            self.cfg["board_snapshot_path"],
            self.cfg["board_max_snapshot_age_seconds"],
            now,
        )
        if rows is None:
            log.warning("board snapshot missing or stale; keeping %d tokens",
                        len(self.watchlist))
            return
        new = select_watchlist(rows, self.cfg["pinned"], self.positions, self.cfg, now)
        added = [t for t in new if t not in self.watchlist]
        dropped = [t for t in self.watchlist if t not in new]
        for t in added:
            self.hist.setdefault(t, TokenHistory(self.maxlen))
        for t in dropped:
            self.hist.pop(t, None)  # no position (those are always kept)
        self.watchlist = new
        if added or dropped:
            log.info("watchlist now %d tokens (+%s -%s)", len(new),
                     ",".join(t[:6] for t in added) or "0",
                     ",".join(t[:6] for t in dropped) or "0")

    # ---- main step ---------------------------------------------------------
    async def poll_all(self):
        """One batched quote_many() call for the whole watchlist - the
        shared oracle's own chunking (30 mints/request) and 429-retry
        apply automatically, same as every other caller in this package.
        A fetch failure already degrades to quote=None per mint (the
        oracle catches its own network/HTTP errors), so each token's own
        step() is still isolated here only against a logic bug in the
        scoring/rule functions below, not against a shared fetch failure."""
        self.refresh_watchlist()
        quotes = await self.oracle.quote_many(self.watchlist, chain="solana")
        for token in self.watchlist:
            try:
                self.step(token, quotes.get(token))
            except Exception:
                log.exception("%s: unexpected error", token[:6])

    def step(self, token, quote):
        snap = snapshot_from_quote(quote)
        if snap is None or snap.price <= 0:
            log.warning("%s: no usable data this poll, holding", token[:6])
            return  # never act on missing data
        h = self.hist[token]
        h.add(snap)

        if h.minutes_observed() < self.cfg["warmup_minutes"]:
            log.info("%s: warming up (%.1f min)", token[:6], h.minutes_observed())
            return

        score, info = health_score(h)
        prof = profile_for(score)
        pos = self.positions.get(token)
        if pos is None:
            self._maybe_enter(token, h, snap, score, info)
        else:
            self._manage(token, pos, h, snap, score, info, prof)

    def _is_repeat_offender(self, token, now):
        """True once a token has lost this many times within the window -
        it stops being re-entered for the rest of that window instead of
        paying to re-learn the same lesson (e.g. Fartcoin's liquidity-
        danger flag: three losses in a row, 30-minute cooldowns apart,
        before this fix)."""
        cutoff = now - self.cfg["repeat_loss_window_hours"] * 3600
        recent = [t for t in self.loss_history.get(token, []) if t >= cutoff]
        return len(recent) >= self.cfg["repeat_loss_block_count"]

    def _maybe_enter(self, token, h, snap, score, info):
        if not self.cfg["auto_enter"]:
            return
        since_exit = (snap.ts - self.cooldown.get(token, 0)) / 60
        checks = {
            "health": score >= self.cfg["entry_min_health"],
            "buyers": info["buy_pressure"] >= 0.52,
            "volume": info["vol_trend"] >= 0.8,
            "trend": snap.price > h.avg("price", 15),
            "liq_stable": info["liq_trend_1h"] >= -0.05,
            "no_danger": not danger_signals(h),
            "cooldown": since_exit >= self.cfg["reentry_cooldown_min"],
            "not_repeat_offender": not self._is_repeat_offender(token, snap.ts),
        }
        log.info("%s: price $%.8g health %.0f | entry checks failing: %s",
                 token[:6], snap.price, score,
                 [k for k, ok in checks.items() if not ok] or "none")
        if all(checks.values()):
            usd = self.cfg["position_usd"]
            qty = self.ex.buy(token, usd, snap)
            self.positions[token] = Position(qty=qty, cost_usd=usd, peak=snap.price,
                                             opened=snap.ts, last_high=snap.ts)
            log.info("%s: BUY $%.2f @ $%.8g (health %.0f)", token[:6], usd, snap.price, score)

    def _manage(self, token, pos, h, snap, score, info, prof):
        price = snap.price
        if price > pos.peak:
            pos.peak, pos.last_high = price, snap.ts

        # 1. Danger: exit now
        danger = danger_signals(h)
        if danger:
            return self._close(token, pos, snap, "DANGER: " + "; ".join(danger))

        # 2. Stop distance scales with volatility and health; warnings tighten it
        warns = warning_signals(h, info)
        mu = h.move_unit()
        dist = clamp(prof.stop_k * mu, prof.min_stop, prof.max_stop)
        confirm = prof.confirm
        if warns:
            dist, confirm = max(dist * 0.6, 0.05), max(1, confirm - 2)
        stop = pos.peak * (1 - dist)
        if pos.tps_hit:  # after taking profit, the rest never turns into a loss
            stop = max(stop, pos.avg_entry * 1.02)
        gain = price / pos.avg_entry - 1

        log.info("%s: $%.8g gain %+.1f%% | health %.0f | stop $%.8g (%d/%d) | liq $%.0f%s",
                 token[:6], price, gain * 100, score, stop, pos.below_count, confirm,
                 snap.liquidity, (" | warn: " + ", ".join(warns)) if warns else "")

        # 3. Take profits in steps
        while pos.tps_hit < len(prof.tp_levels) and gain >= prof.tp_levels[pos.tps_hit][0]:
            frac = prof.tp_levels[pos.tps_hit][1]
            self._sell(token, pos, snap, frac, f"take-profit {pos.tps_hit + 1} at {gain:+.0%}")
            pos.tps_hit += 1

        # 4. Trailing stop, with confirmation so one bad reading doesn't sell
        pos.below_count = pos.below_count + 1 if price < stop else 0
        if price < pos.peak * (1 - 1.5 * dist):
            return self._close(token, pos, snap, "fell far past stop")
        if pos.below_count >= confirm:
            return self._close(token, pos, snap, f"trailing stop confirmed x{confirm}")

        # 5. Stalling only counts when the coin is also weakening
        stalled = (snap.ts - pos.last_high) / 60
        if warns and stalled >= prof.stall_minutes:
            return self._close(token, pos, snap,
                               f"no new high for {stalled:.0f}m and {', '.join(warns)}")

        # 6. Buy the dip: healthy coin, normal-sized dip, liquidity holding, bounce starting
        drawdown = 1 - price / pos.peak
        add_usd = self.cfg["position_usd"] * self.cfg["dip_add_fraction"]
        if (self.cfg["dip_buys_enabled"]
                and pos.adds < prof.max_adds and not warns and pos.tps_hit == 0
                and snap.ts - pos.last_add >= 600
                and 1.5 * mu <= drawdown <= min(3.5, prof.stop_k - 1) * mu
                and h.pct_change("liquidity", 15) > -0.05
                and info["buy_pressure"] >= 0.48
                and price >= h.snaps[-2].price
                and pos.cost_usd + add_usd <= self.cfg["max_total_usd_per_token"]):
            qty = self.ex.buy(token, add_usd, snap)
            pos.qty += qty
            pos.cost_usd += add_usd
            pos.adds += 1
            pos.last_add = snap.ts
            log.info("%s: DIP BUY #%d $%.2f @ $%.8g (dip %.1f%%, new avg $%.8g)",
                     token[:6], pos.adds, add_usd, price, drawdown * 100, pos.avg_entry)

    def _sell(self, token, pos, snap, frac, reason):
        qty = pos.qty * frac
        usd = self.ex.sell(token, qty, snap)
        cost = pos.cost_usd * frac
        pos.qty -= qty
        pos.cost_usd -= cost
        pos.realized_usd += usd - cost
        log.info("%s: SELL %.0f%% @ $%.8g -> $%.2f (%s)",
                 token[:6], frac * 100, snap.price, usd, reason)

    def _close(self, token, pos, snap, reason):
        self._sell(token, pos, snap, 1.0, reason)
        self.total_pnl += pos.realized_usd
        if snap.ts >= PNL_SPLIT_AT:
            self.pnl_since_split += pos.realized_usd
        else:
            self.pnl_before_split += pos.realized_usd
        log.info("%s: CLOSED. Trade P&L $%+.2f | total $%+.2f",
                 token[:6], pos.realized_usd, self.total_pnl)
        del self.positions[token]
        self.cooldown[token] = snap.ts
        if pos.realized_usd < 0:
            losses = self.loss_history.setdefault(token, [])
            losses.append(snap.ts)
            cutoff = snap.ts - self.cfg["repeat_loss_window_hours"] * 3600
            self.loss_history[token] = [t for t in losses if t >= cutoff]


async def run():
    bot = Bot(CONFIG, PaperExecutor())
    log.info("Tracking %d pinned tokens%s (paper mode)", len(CONFIG["pinned"]),
             " + board tokens" if CONFIG["dynamic_watchlist"] else "")
    cycle = 0
    while True:
        await bot.poll_all()
        bot.save()
        cycle += 1
        # Every ~10 minutes: the $50/dip-buy era (before 20:32) dominates the
        # raw total, so status reports it split instead of one misleading
        # headline number.
        if cycle % max(1, round(600 / CONFIG["poll_seconds"])) == 0:
            log.info(
                "PNL split: before 20:32 $%+.2f | since 20:32 $%+.2f | total $%+.2f | open %d",
                bot.pnl_before_split, bot.pnl_since_split, bot.total_pnl, len(bot.positions),
            )
        await asyncio.sleep(CONFIG["poll_seconds"])


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(CONFIG["log_file"])],
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
