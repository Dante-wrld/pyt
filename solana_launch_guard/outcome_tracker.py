"""Record what happened to every token the bot decided about.

The trading databases only keep the launch event and the decision, so there is
no way to tell afterwards whether a rejected token doubled or an accepted one
rugged. This tracker runs as its own process beside the bot:

- It opens the bot's databases strictly read-only (``mode=ro``) and copies new
  decisions into a separate outcomes database. It holds no keys, builds no
  transactions, and cannot change anything the live paths read.
- For each newly decided mint it schedules price samples: one immediately (the
  realistic entry), a dense series for tokens the bot accepted or a live agent
  considered, and a set of fixed horizons for everything.
- Rejected launches are sampled at a fixed, deterministic rate (by mint hash)
  so the request budget stays bounded while each rejection reason still gets
  an unbiased sample.

Quotes come from DEX Screener's batched ``tokens/v1`` endpoint, 30 mints per
request. A missing or empty quote is stored as ``found=0`` rather than skipped,
because a token vanishing is itself an outcome.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import certifi

from .evaluation import Observation, TrackedDecision

LOGGER = logging.getLogger(__name__)

DEXSCREENER_BATCH_URL = "https://api.dexscreener.com/tokens/v1/solana/"
BATCH_SIZE = 30
DEFAULT_HORIZONS = (300.0, 900.0, 3600.0, 4 * 3600.0, 24 * 3600.0)
LEDGER_TRACKED_STATES = frozenset(
    {"PROPOSAL", "APPROVED", "BLOCKED", "BUY_READY", "BUY_ZONE_SKIPPED", "CONFIRMED"}
)


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    reject_sample_rate: float = 0.10
    horizons_seconds: tuple[float, ...] = DEFAULT_HORIZONS
    dense_interval_seconds: float = 60.0
    dense_window_seconds: float = 3600.0
    max_decision_lag_seconds: float = 300.0
    requests_per_cycle: int = 10


@dataclass(frozen=True, slots=True)
class Quote:
    mint: str
    price_usd: float | None
    liquidity_usd: float | None


@dataclass(frozen=True, slots=True)
class NewDecision:
    source: str
    source_id: int
    mint: str
    decided_at: float
    label: str
    accepted: bool | None
    reasons: tuple[str, ...]


def sampled(mint: str, rate: float) -> bool:
    """Deterministic per-mint sampling, so reruns pick the same tokens."""
    if rate >= 1:
        return True
    if rate <= 0:
        return False
    digest = hashlib.sha256(mint.encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < rate


def parse_batch(payload: Any, requested: Iterable[str]) -> dict[str, Quote]:
    """Pick each mint's deepest pool; mints absent from the reply map to an
    empty Quote so callers record them as not found."""
    best: dict[str, tuple[float, Quote]] = {}
    for pair in payload if isinstance(payload, list) else []:
        if not isinstance(pair, dict) or pair.get("chainId") != "solana":
            continue
        mint = str((pair.get("baseToken") or {}).get("address") or "")
        if not mint:
            continue
        try:
            price = float(pair.get("priceUsd") or 0.0)
        except (TypeError, ValueError):
            continue
        try:
            liquidity = float((pair.get("liquidity") or {}).get("usd") or 0.0)
        except (TypeError, ValueError):
            liquidity = 0.0
        if price <= 0:
            continue
        if mint not in best or liquidity > best[mint][0]:
            best[mint] = (liquidity, Quote(mint, price, liquidity))
    return {
        mint: best[mint][1] if mint in best else Quote(mint, None, None)
        for mint in requested
    }


class DexScreenerBatchClient:
    def __init__(self, timeout_seconds: float = 15.0) -> None:
        self._ssl = ssl.create_default_context(cafile=certifi.where())
        self._timeout = timeout_seconds

    async def quotes(self, mints: Sequence[str]) -> dict[str, Quote]:
        return await asyncio.to_thread(self._quotes, list(mints))

    def _quotes(self, mints: list[str]) -> dict[str, Quote]:
        request = urllib.request.Request(
            DEXSCREENER_BATCH_URL + ",".join(mints[:BATCH_SIZE]),
            headers={"Accept": "application/json", "User-Agent": "launch-guard-eval"},
        )
        with urllib.request.urlopen(
            request, timeout=self._timeout, context=self._ssl
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return parse_batch(payload, mints[:BATCH_SIZE])


def _open_read_only(path: str | Path) -> sqlite3.Connection | None:
    resolved = Path(path)
    if not resolved.exists():
        return None
    return sqlite3.connect(f"{resolved.resolve().as_uri()}?mode=ro", uri=True)


def _iso_to_epoch(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def read_launch_decisions(path: str | Path, after_id: int) -> list[NewDecision]:
    """Risk-engine decisions from launch_guard.db (read-only)."""
    connection = _open_read_only(path)
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT id, decided_at, mint, accepted, reasons_json FROM decisions "
            "WHERE id > ? ORDER BY id LIMIT 5000",
            (after_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()
    decisions: list[NewDecision] = []
    for row_id, decided_at, mint, accepted, reasons_json in rows:
        try:
            reasons = tuple(str(r) for r in json.loads(reasons_json or "[]"))
            at = _iso_to_epoch(decided_at)
        except (ValueError, TypeError):
            continue
        decisions.append(
            NewDecision(
                "launch", row_id, mint, at,
                "ACCEPTED" if accepted else "REJECTED", bool(accepted), reasons,
            )
        )
    return decisions


def read_buy_signals(path: str | Path, after_id: int) -> list[NewDecision]:
    """Each move into a buy decision (BUY ZONE, MOMENTUM BUY, ...) from the
    board, labelled by decision so signals can be compared head to head.
    Solana only: the quote source is DEX Screener's Solana endpoint."""
    connection = _open_read_only(path)
    if connection is None:
        return []
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(buy_signals)")
        }
        sources = "sources" if "sources" in columns else "NULL"
        rows = connection.execute(
            "SELECT id, signaled_at, mint, decision, reason, live_blocked_reason, "
            f"{sources} FROM buy_signals WHERE id > ? AND chain = 'solana' "
            "ORDER BY id LIMIT 5000",
            (after_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()
    decisions: list[NewDecision] = []
    for row_id, signaled_at, mint, decision, reason, blocked, found_by in rows:
        try:
            at = _iso_to_epoch(signaled_at)
        except (ValueError, TypeError):
            continue
        live = f"live: {blocked}" if blocked else "live: allowed"
        # One sorted tag per signal, so a token two feeds found forms its own
        # group ("board,leader-held") instead of being counted twice.
        source = (
            f"source: {','.join(sorted(found_by.split(',')))}" if found_by else None
        )
        reasons = tuple(r for r in (reason, live, source) if r)
        decisions.append(
            NewDecision("signal", row_id, mint, at, decision, None, reasons)
        )
    return decisions


def read_signal_tags(path: str | Path, ids: Sequence[int]) -> dict[int, str]:
    """Candle patterns for the given buy_signals ids that have been tagged."""
    connection = _open_read_only(path)
    if connection is None or not ids:
        if connection is not None:
            connection.close()
        return {}
    try:
        marks = ",".join("?" * len(ids))
        rows = connection.execute(
            f"SELECT id, candle_pattern FROM buy_signals WHERE id IN ({marks}) "
            "AND candle_pattern IS NOT NULL",
            tuple(ids),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        connection.close()
    return {int(row_id): str(pattern) for row_id, pattern in rows}


def read_wallet_buys(
    path: str | Path, after_id: int, wallet_labels: dict[str, str]
) -> list[NewDecision]:
    """Buys recorded for CopyFomo's wallet and its leaders' wallets, so their
    real entries can be replayed through the same exit simulations.
    Labelled 'COPYFOMO' or 'leader:<name>'."""
    if not wallet_labels:
        return []
    connection = _open_read_only(path)
    if connection is None:
        return []
    try:
        marks = ",".join("?" * len(wallet_labels))
        rows = connection.execute(
            "SELECT id, seen_at, wallet, mint FROM wallet_trades "
            f"WHERE id > ? AND side = 'BUY' AND wallet IN ({marks}) "
            "ORDER BY id LIMIT 5000",
            (after_id, *wallet_labels),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()
    decisions: list[NewDecision] = []
    for row_id, seen_at, wallet, mint in rows:
        try:
            at = _iso_to_epoch(seen_at)
        except (ValueError, TypeError):
            continue
        decisions.append(
            NewDecision("wallet", row_id, mint, at, wallet_labels[wallet], None, ())
        )
    return decisions


def read_scored_candidates(path: str | Path, after_id: int) -> list[NewDecision]:
    """Candidates the intelligence layer accepted onto the board (read-only).

    This is the only record of established-token candidates from the Solana
    momentum feed, which never writes to ``decisions``. Labelled by tier.
    """
    connection = _open_read_only(path)
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT id, scored_at, mint, tier, total_score FROM intelligence_scores "
            "WHERE id > ? ORDER BY id LIMIT 5000",
            (after_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()
    decisions: list[NewDecision] = []
    for row_id, scored_at, mint, tier, score in rows:
        try:
            at = _iso_to_epoch(scored_at)
        except (ValueError, TypeError):
            continue
        decisions.append(
            NewDecision(
                "scored", row_id, mint, at, str(tier), None, (f"score {score}",)
            )
        )
    return decisions


def read_ledger_decisions(path: str | Path, after_id: int) -> list[NewDecision]:
    """Live-trial agent decisions (read-only), labelled 'agent:STATE'."""
    connection = _open_read_only(path)
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT rowid, at, agent, mint, state, reason FROM decisions "
            "WHERE rowid > ? ORDER BY rowid LIMIT 5000",
            (after_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()
    return [
        NewDecision(
            "ledger", row_id, mint, float(at), f"{agent}:{state}", None, (reason,)
        )
        for row_id, at, agent, mint, state, reason in rows
        if state in LEDGER_TRACKED_STATES
    ]


class OutcomeStore:
    def __init__(self, path: str | Path) -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS tracked_decisions (
                source TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                mint TEXT NOT NULL,
                decided_at REAL NOT NULL,
                label TEXT NOT NULL,
                accepted INTEGER,
                reasons_json TEXT NOT NULL,
                PRIMARY KEY (source, source_id)
            );
            CREATE INDEX IF NOT EXISTS idx_tracked_mint
                ON tracked_decisions(mint);
            CREATE TABLE IF NOT EXISTS schedule (
                mint TEXT NOT NULL,
                due_at REAL NOT NULL,
                PRIMARY KEY (mint, due_at)
            );
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mint TEXT NOT NULL,
                observed_at REAL NOT NULL,
                price_usd REAL,
                liquidity_usd REAL,
                found INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_observations_mint
                ON observations(mint, observed_at);
            CREATE TABLE IF NOT EXISTS cursors (
                source TEXT PRIMARY KEY,
                last_id INTEGER NOT NULL
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def cursor(self, source: str) -> int:
        row = self.connection.execute(
            "SELECT last_id FROM cursors WHERE source = ?", (source,)
        ).fetchone()
        return int(row[0]) if row else 0

    def untagged_signal_ids(self, limit: int = 500) -> list[int]:
        rows = self.connection.execute(
            "SELECT source_id FROM tracked_decisions WHERE source = 'signal' "
            "AND reasons_json NOT LIKE '%candle: %' "
            "ORDER BY source_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [int(row[0]) for row in rows]

    def add_signal_tags(self, tags: dict[int, str]) -> None:
        with self.connection:
            for source_id, pattern in tags.items():
                row = self.connection.execute(
                    "SELECT reasons_json FROM tracked_decisions "
                    "WHERE source = 'signal' AND source_id = ?",
                    (source_id,),
                ).fetchone()
                if row is None:
                    continue
                reasons = [*json.loads(row[0]), f"candle: {pattern}"]
                self.connection.execute(
                    "UPDATE tracked_decisions SET reasons_json = ? "
                    "WHERE source = 'signal' AND source_id = ?",
                    (json.dumps(reasons), source_id),
                )

    def recently_tracked(
        self, source: str, label: str, mint: str, since: float
    ) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM tracked_decisions WHERE source = ? AND label = ? "
            "AND mint = ? AND decided_at >= ? LIMIT 1",
            (source, label, mint, since),
        ).fetchone() is not None

    def is_scheduled_mint(self, mint: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM tracked_decisions WHERE mint = ? LIMIT 1", (mint,)
            ).fetchone()
            is not None
        )

    def record(
        self,
        source: str,
        decisions: Sequence[NewDecision],
        tracked: Sequence[tuple[NewDecision, Sequence[float]]],
    ) -> None:
        with self.connection:
            for decision, due_times in tracked:
                self.connection.execute(
                    "INSERT OR IGNORE INTO tracked_decisions VALUES (?,?,?,?,?,?,?)",
                    (
                        decision.source, decision.source_id, decision.mint,
                        decision.decided_at, decision.label,
                        None if decision.accepted is None else int(decision.accepted),
                        json.dumps(decision.reasons),
                    ),
                )
                self.connection.executemany(
                    "INSERT OR IGNORE INTO schedule VALUES (?, ?)",
                    [(decision.mint, due) for due in due_times],
                )
            if decisions:
                self.connection.execute(
                    "INSERT INTO cursors VALUES (?, ?) ON CONFLICT(source) "
                    "DO UPDATE SET last_id = excluded.last_id",
                    (source, max(d.source_id for d in decisions)),
                )

    def due_mints(self, now: float, limit: int) -> list[str]:
        rows = self.connection.execute(
            "SELECT mint FROM schedule WHERE due_at <= ? GROUP BY mint "
            "ORDER BY MIN(due_at) LIMIT ?",
            (now, limit),
        ).fetchall()
        return [row[0] for row in rows]

    def save_quotes(self, quotes: dict[str, Quote], now: float) -> None:
        with self.connection:
            for mint, quote in quotes.items():
                self.connection.execute(
                    "INSERT INTO observations(mint, observed_at, price_usd, "
                    "liquidity_usd, found) VALUES (?, ?, ?, ?, ?)",
                    (
                        mint, now, quote.price_usd, quote.liquidity_usd,
                        int(quote.price_usd is not None),
                    ),
                )
                self.connection.execute(
                    "DELETE FROM schedule WHERE mint = ? AND due_at <= ?",
                    (mint, now),
                )

    def load(self) -> list[TrackedDecision]:
        observations: dict[str, list[Observation]] = {}
        for mint, at, price, liquidity, found in self.connection.execute(
            "SELECT mint, observed_at, price_usd, liquidity_usd, found "
            "FROM observations ORDER BY mint, observed_at"
        ):
            observations.setdefault(mint, []).append(
                Observation(at, price, liquidity, bool(found))
            )
        loaded: list[TrackedDecision] = []
        for source, mint, at, label, reasons_json in self.connection.execute(
            "SELECT source, mint, decided_at, label, reasons_json "
            "FROM tracked_decisions ORDER BY decided_at"
        ):
            loaded.append(
                TrackedDecision(
                    source, mint, at, label, tuple(json.loads(reasons_json)),
                    tuple(o for o in observations.get(mint, ()) if o.observed_at >= at),
                )
            )
        return loaded

    def pending_count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) FROM schedule").fetchone()
        return int(row[0])


def plan_samples(
    decision: NewDecision, now: float, config: TrackerConfig, dense: bool
) -> list[float]:
    due = {now}
    if dense:
        step = max(config.dense_interval_seconds, 1.0)
        offset = step
        while offset <= config.dense_window_seconds:
            due.add(decision.decided_at + offset)
            offset += step
    for horizon in config.horizons_seconds:
        due.add(decision.decided_at + horizon)
    return sorted(t for t in due if t >= now)


class OutcomeTracker:
    def __init__(
        self,
        store: OutcomeStore,
        client: DexScreenerBatchClient,
        config: TrackerConfig,
        *,
        launch_db: str | Path | None,
        ledger_db: str | Path | None,
        wallet_labels: dict[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.client = client
        self.config = config
        self.launch_db = launch_db
        self.ledger_db = ledger_db
        self.wallet_labels = dict(wallet_labels or {})
        self.clock = clock

    def ingest(self) -> int:
        now = self.clock()
        added = 0
        sources: list[tuple[str, list[NewDecision]]] = []
        if self.launch_db:
            sources.append(
                ("launch", read_launch_decisions(
                    self.launch_db, self.store.cursor("launch")))
            )
            sources.append(
                ("scored", read_scored_candidates(
                    self.launch_db, self.store.cursor("scored")))
            )
            sources.append(
                ("signal", read_buy_signals(
                    self.launch_db, self.store.cursor("signal")))
            )
            sources.append(
                ("wallet", read_wallet_buys(
                    self.launch_db, self.store.cursor("wallet"), self.wallet_labels))
            )
        if self.ledger_db:
            sources.append(
                ("ledger", read_ledger_decisions(
                    self.ledger_db, self.store.cursor("ledger")))
            )
        for source, decisions in sources:
            tracked: list[tuple[NewDecision, Sequence[float]]] = []
            seen: set[str] = set()
            seen_wallet: set[tuple[str, str]] = set()
            for decision in decisions:
                if now - decision.decided_at > self.config.max_decision_lag_seconds:
                    continue  # too stale for a realistic entry quote
                if decision.accepted is False and not sampled(
                    decision.mint, self.config.reject_sample_rate
                ):
                    continue
                if decision.source == "wallet" and (
                    (decision.label, decision.mint) in seen_wallet
                    or self.store.recently_tracked(
                        "wallet", decision.label, decision.mint,
                        decision.decided_at - 24 * 3600,
                    )
                ):
                    continue  # a top-up of a position already being tracked
                seen_wallet.add((decision.label, decision.mint))
                # Signals and real wallet buys are measured from their own
                # moment, so they get their own samples even for a tracked mint.
                already = decision.source not in ("signal", "wallet") and (
                    decision.mint in seen or self.store.is_scheduled_mint(decision.mint)
                )
                dense = decision.accepted is not False
                samples = [] if already else plan_samples(
                    decision, now, self.config, dense
                )
                tracked.append((decision, samples))
                seen.add(decision.mint)
                added += 0 if already else 1
            self.store.record(source, decisions, tracked)
        if self.launch_db:
            # Tags arrive a few seconds after a signal row is written, often
            # after this tracker already copied the row; fill them in later.
            untagged = self.store.untagged_signal_ids()
            if untagged:
                self.store.add_signal_tags(read_signal_tags(self.launch_db, untagged))
        return added

    async def sample_once(self) -> int:
        now = self.clock()
        mints = self.store.due_mints(now, self.config.requests_per_cycle * BATCH_SIZE)
        fetched = 0
        for start in range(0, len(mints), BATCH_SIZE):
            batch = mints[start : start + BATCH_SIZE]
            try:
                quotes = await self.client.quotes(batch)
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                # Leave the schedule in place; the next cycle retries.
                LOGGER.warning("quote batch failed (%s); retrying next cycle", exc)
                break
            self.store.save_quotes(quotes, self.clock())
            fetched += len(batch)
        return fetched

    async def run(self, poll_seconds: float = 20.0) -> None:
        while True:
            added = self.ingest()
            fetched = await self.sample_once()
            LOGGER.info(
                "tracked +%d new mints, sampled %d, %d samples pending",
                added, fetched, self.store.pending_count(),
            )
            await asyncio.sleep(poll_seconds)
