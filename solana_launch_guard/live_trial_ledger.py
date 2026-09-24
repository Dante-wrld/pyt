"""Durable, fail-closed capital and order reservations for a future live trial.

This module never signs, simulates, or broadcasts a transaction. Starting the
clock must be an explicit action after wallet and sell-path preflight.
"""
from __future__ import annotations

import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any


BUY_AGENTS = ("hunter-v1", "copy-v1")
BUY_CAP_CENTS = 500
AGENT_BUDGET_CENTS = 3000
TRIAL_SECONDS = 8 * 3600


class TrialHalted(ValueError):
    """A trial guard blocks further orders."""


class LiveTrialLedger:
    """One persistent eight-hour session. A brand new session is never
    automatic - it always needs a fresh ledger file - but resuming the
    SAME still-valid session after an unplanned process restart is, so an
    operator's only recovery action (running --start again) never has to
    come at the cost of forgetting an open position - see start()."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS trial (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                started_at REAL NOT NULL, deadline REAL NOT NULL,
                halted INTEGER NOT NULL DEFAULT 0, reason TEXT
            );
            CREATE TABLE IF NOT EXISTS orders (
                intent TEXT PRIMARY KEY, agent TEXT NOT NULL,
                side TEXT NOT NULL, mint TEXT NOT NULL,
                reserved_cents INTEGER NOT NULL DEFAULT 0,
                executed_cents INTEGER, proceeds_cents INTEGER,
                state TEXT NOT NULL, signature TEXT, realized_cents INTEGER,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at REAL NOT NULL, agent TEXT NOT NULL, mint TEXT NOT NULL,
                state TEXT NOT NULL, reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS positions (
                agent TEXT NOT NULL, mint TEXT NOT NULL,
                quantity_raw INTEGER NOT NULL, decimals INTEGER NOT NULL,
                cost_cents INTEGER NOT NULL, entry_price REAL NOT NULL,
                entry_liquidity_usd REAL NOT NULL,
                peak_price REAL NOT NULL, current_price REAL NOT NULL,
                opened_at REAL NOT NULL, updated_at REAL NOT NULL,
                principal_recovered INTEGER NOT NULL DEFAULT 0,
                second_stage_taken INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(agent,mint)
            );
            CREATE TABLE IF NOT EXISTS closed_positions (
                agent TEXT NOT NULL, mint TEXT NOT NULL,
                exit_price REAL NOT NULL, closed_at REAL NOT NULL,
                PRIMARY KEY(agent,mint)
            );
        """)
        if "realized_cents" not in {row[1] for row in self.db.execute("PRAGMA table_info(orders)")}:
            self.db.execute("ALTER TABLE orders ADD COLUMN realized_cents INTEGER")
        position_columns = {row[1] for row in self.db.execute("PRAGMA table_info(positions)")}
        for column in ("principal_recovered", "second_stage_taken"):
            if column not in position_columns:
                self.db.execute(f"ALTER TABLE positions ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        if "origin" not in position_columns:
            self.db.execute("ALTER TABLE positions ADD COLUMN origin TEXT NOT NULL DEFAULT 'fresh'")
        if "decision" not in {row[1] for row in self.db.execute("PRAGMA table_info(orders)")}:
            self.db.execute("ALTER TABLE orders ADD COLUMN decision TEXT")

    def close(self) -> None:
        self.db.close()

    def _begin(self) -> None:
        self.db.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _check_kill_switch() -> None:
        if os.getenv("AGENT_LIVE_KILL_SWITCH", "true").lower() != "false":
            raise TrialHalted("agent kill switch is active")
        # An already-running process cannot see edits to its parent shell's
        # environment. Read only the kill-switch key from the current .env.
        try:
            lines = Path(".env").read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        for line in lines:
            if line.strip().startswith("AGENT_LIVE_KILL_SWITCH="):
                if line.split("=", 1)[1].strip().strip('"\'').lower() != "false":
                    raise TrialHalted("kill switch is active in .env")

    def _guard(self, now: float) -> None:
        row = self.db.execute("SELECT deadline, halted FROM trial WHERE id=1").fetchone()
        if not row or row[1] or not math.isfinite(now) or now >= row[0]:
            raise TrialHalted("trial is absent, halted, or expired")
        self._check_kill_switch()
        if self.path.with_suffix(self.path.suffix + ".stop").exists():
            raise TrialHalted("operator stop file exists")

    def assert_active(self, *, now: float | None = None) -> None:
        self._guard(time.time() if now is None else now)

    def log(self, *, agent: str, mint: str, state: str, reason: str) -> None:
        if agent not in (*BUY_AGENTS, "portfolio-v1") or not state or not reason:
            raise ValueError("attributed decision, state, and reason are required")
        self.db.execute(
            "INSERT INTO decisions(at,agent,mint,state,reason) VALUES(?,?,?,?,?)",
            (time.time(), agent, mint[:100], state[:40], reason[:1000]),
        )

    def unresolved(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT intent,agent,side,mint,state,signature FROM orders "
            "WHERE state IN ('RESERVED','SUBMITTED','UNCERTAIN') ORDER BY created_at"
        ).fetchall()
        return [dict(zip(("intent", "agent", "side", "mint", "state", "signature"), row)) for row in rows]

    def started_at(self) -> float:
        """A per-session value, unlike path.stem: the active ledger is always
        recreated at the same filename after the old one is archived away, so
        the stem alone can't tell this session apart from a previous one."""
        row = self.db.execute("SELECT started_at FROM trial WHERE id=1").fetchone()
        return row[0]

    def model_request_count(self) -> int:
        return int(self.db.execute(
            "SELECT COUNT(*) FROM decisions WHERE state='MODEL_REQUEST'"
        ).fetchone()[0])

    def start(self, *, now: float | None = None) -> None:
        """Initialize once, or resume the same still-valid session after an
        unplanned process restart (crash, OOM kill, hardware failure) -
        caller must independently pass all live preflights either way.

        A trial row that is already halted (an operator's deliberate
        --live-trial-stop, or an internal halt() such as an expired
        deadline or the model-request cap) still refuses, exactly as
        before: a genuinely NEW session remains an explicit, non-automatic
        decision, made against a fresh ledger file. But one that is
        neither halted nor past its own deadline is, by definition, the
        SAME session continuing after an interruption, not a new one - and
        refusing that forced the only available recovery path to be
        archiving the whole file and starting over, which silently wiped
        every open position's entry price, peak price, and profit-ladder
        stage. positions/orders/decisions are never touched here, so
        resuming preserves them exactly as an in-place reconnect would.
        """
        at = time.time() if now is None else now
        if not math.isfinite(at) or at <= 0:
            raise ValueError("invalid trial start time")
        self._check_kill_switch()
        if self.path.with_suffix(self.path.suffix + ".stop").exists():
            raise TrialHalted("operator stop file exists")
        self._begin()
        try:
            existing = self.db.execute("SELECT deadline, halted FROM trial WHERE id=1").fetchone()
            if existing is not None:
                deadline, halted = existing
                if halted or at >= deadline:
                    raise TrialHalted("trial already exists; a new session is not automatic")
                self.db.commit()
                return
            self.db.execute("INSERT INTO trial(id,started_at,deadline) VALUES(1,?,?)", (at, at + TRIAL_SECONDS))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def halt(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("halt reason is required")
        self.db.execute("UPDATE trial SET halted=1,reason=? WHERE id=1", (reason[:300],))

    def stop(self) -> None:
        """Durable operator stop, visible to processes with a cached environment."""
        path = self.path.with_suffix(self.path.suffix + ".stop")
        descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        self.halt("operator requested stop")

    def _spent(self, agent: str) -> int:
        row = self.db.execute(
            "SELECT COALESCE(SUM(reserved_cents),0) FROM orders "
            "WHERE agent=? AND side='BUY' AND state IN ('RESERVED','SUBMITTED','UNCERTAIN','CONFIRMED')",
            (agent,),
        ).fetchone()
        return int(row[0])

    def daily_realized_cents(self, agent: str, *, now: float | None = None) -> int:
        at = time.time() if now is None else now
        start = math.floor(at / 86400) * 86400
        return int(self.db.execute(
            "SELECT COALESCE(SUM(realized_cents),0) FROM orders WHERE agent=? "
            "AND side='SELL' AND state='CONFIRMED' AND created_at >= ?",
            (agent, start),
        ).fetchone()[0])

    def reserve_buy(self, *, intent: str, agent: str, mint: str,
                    requested_cents: int, approved_cents: int, now: float | None = None) -> None:
        at = time.time() if now is None else now
        self._begin()
        try:
            self._guard(at)
            if agent not in BUY_AGENTS or not intent or not mint:
                raise ValueError("only attributed hunter/copy trades can reserve new capital")
            if type(requested_cents) is not int or type(approved_cents) is not int or not (
                0 < approved_cents <= requested_cents and approved_cents <= BUY_CAP_CENTS
            ):
                raise ValueError("buy must remain within request and $5 order cap")
            if self._spent(agent) + approved_cents > AGENT_BUDGET_CENTS:
                raise ValueError("agent gross-buy budget exhausted")
            if self.db.execute("SELECT 1 FROM positions WHERE mint=?", (mint,)).fetchone():
                raise ValueError("token is already owned in this trial")
            if self.db.execute("SELECT 1 FROM orders WHERE mint=? AND side='BUY' "
                               "AND state IN ('RESERVED','SUBMITTED','UNCERTAIN')", (mint,)).fetchone():
                raise ValueError("token buy has an unresolved trial intent")
            if self.db.execute("SELECT COUNT(*) FROM positions WHERE agent=?", (agent,)).fetchone()[0] >= 2:
                raise ValueError("maximum two live positions per agent")
            self.db.execute(
                "INSERT INTO orders(intent,agent,side,mint,reserved_cents,state,created_at) "
                "VALUES(?,?,'BUY',?,?,'RESERVED',?)",
                (intent, agent, mint, approved_cents, at),
            )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def reserve_sell(self, *, intent: str, agent: str, mint: str, now: float | None = None) -> None:
        at = time.time() if now is None else now
        self._begin()
        try:
            self._guard(at)
            if agent not in (*BUY_AGENTS, "portfolio-v1") or not intent or not mint:
                raise ValueError("invalid owned-position sell intent")
            self.db.execute(
                "INSERT INTO orders(intent,agent,side,mint,state,created_at) "
                "VALUES(?,?,'SELL',?,'RESERVED',?)", (intent, agent, mint, at),
            )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def transition(self, intent: str, state: str, *, signature: str | None = None,
                   executed_cents: int | None = None, proceeds_cents: int | None = None,
                   verified_on_chain: bool = False,
                   absent_on_chain: bool = False) -> None:
        """Only a verified chain receipt may use CONFIRMED; uncertain spends stay reserved."""
        self._begin()
        try:
            row = self.db.execute(
                "SELECT side,state,reserved_cents FROM orders WHERE intent=?", (intent,)
            ).fetchone()
            if row is None:
                raise ValueError("order intent does not exist")
            side, old, reserved = row
            if (old, state) not in {
                ("RESERVED", "FAILED"), ("RESERVED", "SUBMITTED"),
                ("SUBMITTED", "UNCERTAIN"), ("SUBMITTED", "CONFIRMED"),
                ("UNCERTAIN", "CONFIRMED"), ("UNCERTAIN", "FAILED"),
            }:
                raise ValueError("invalid or duplicate order transition")
            if state == "SUBMITTED" and not signature:
                raise ValueError("submitted transaction requires signature")
            if state == "CONFIRMED" and verified_on_chain is not True:
                raise ValueError("a confirmed order requires an independent on-chain check")
            if old == "UNCERTAIN" and state == "FAILED" and absent_on_chain is not True:
                raise ValueError("an uncertain order cannot release capital without chain reconciliation")
            if state == "CONFIRMED" and (not signature or
                    (side == "BUY" and (type(executed_cents) is not int or not 0 < executed_cents <= reserved)) or
                    (side == "SELL" and (type(proceeds_cents) is not int or proceeds_cents < 0))):
                raise ValueError("confirmation requires a verified signature and bounded fill")
            self.db.execute(
                "UPDATE orders SET state=?,signature=COALESCE(?,signature),"
                "executed_cents=COALESCE(?,executed_cents),proceeds_cents=COALESCE(?,proceeds_cents) "
                "WHERE intent=?",
                (state, signature, executed_cents, proceeds_cents, intent),
            )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def confirm_buy(self, *, intent: str, signature: str, executed_cents: int,
                    quantity_raw: int, decimals: int, entry_price: float,
                    entry_liquidity_usd: float, verified_on_chain: bool,
                    origin: str = "fresh", decision: str | None = None) -> None:
        """Atomically confirm a buy and establish its persistent post-entry peak.

        origin distinguishes a position hunter-v1 sourced itself from the
        live recommendation feed ("fresh") from one portfolio-v1 flagged as
        a previously-closed mint growing again ("regrowth") - kept separate
        so the two have independent open-position caps and a fresh
        discovery is never crowded out by (or crowds out) a regrowth
        re-entry, and vice versa.

        decision is the recommendation-engine label (MOMENTUM BUY, BUY
        NOW, BUY ZONE, EARLY BUY, REGROWTH REBUY) that sourced this buy,
        persisted on the orders row (never deleted, unlike positions) so
        model_performance can later attribute realized P&L back to it -
        the whole point being to see whether the model's approval is
        actually associated with profit, not just added latency.
        """
        if origin not in {"fresh", "regrowth"}:
            raise ValueError("origin must be 'fresh' or 'regrowth'")
        self._begin()
        try:
            row = self.db.execute("SELECT agent,mint,side,state,signature,reserved_cents FROM orders WHERE intent=?", (intent,)).fetchone()
            if (not row or row[2] != "BUY" or row[3] not in {"SUBMITTED", "UNCERTAIN"}
                or row[4] != signature or verified_on_chain is not True
                or type(executed_cents) is not int or not 0 < executed_cents <= row[5]
                or type(quantity_raw) is not int or quantity_raw <= 0
                or type(decimals) is not int or not 0 <= decimals <= 18
                or not math.isfinite(entry_price) or entry_price <= 0
                or not math.isfinite(entry_liquidity_usd) or entry_liquidity_usd <= 0):
                raise ValueError("buy fill has not been independently verified or exceeds reservation")
            self.db.execute("INSERT INTO positions(agent,mint,quantity_raw,decimals,cost_cents,entry_price,"
                            "entry_liquidity_usd,peak_price,current_price,opened_at,updated_at,origin) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                row[0], row[1], quantity_raw, decimals, executed_cents,
                entry_price, entry_liquidity_usd, entry_price, entry_price,
                time.time(), time.time(), origin,
            ))
            self.db.execute("UPDATE orders SET state='CONFIRMED',executed_cents=?,decision=? WHERE intent=?",
                           (executed_cents, decision, intent))
            self.db.execute("DELETE FROM closed_positions WHERE agent=? AND mint=?", (row[0], row[1]))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def positions(self, agent: str | None = None) -> list[dict]:
        # decision (MOMENTUM BUY, EARLY BUY, ...) is read via the orders
        # row it was confirmed on - the same source confirm_buy's own
        # decision param persists to, and for the same reason (see its
        # docstring): orders is append-only, positions is deleted on
        # close, so orders is the only place that can still answer "what
        # kind of buy opened this" without duplicating storage. The most
        # recent CONFIRMED BUY for this (agent, mint) is always the one
        # that opened the position still open now - an earlier round trip
        # on the same mint would have been sold and closed first.
        names = ("agent", "mint", "quantity_raw", "decimals", "cost_cents", "entry_price",
                 "entry_liquidity_usd", "peak_price", "current_price", "opened_at", "updated_at",
                 "principal_recovered", "second_stage_taken", "origin", "decision")
        query = (
            "SELECT p.*, ("
            "  SELECT o.decision FROM orders o"
            "  WHERE o.agent = p.agent AND o.mint = p.mint"
            "    AND o.side = 'BUY' AND o.state = 'CONFIRMED'"
            "  ORDER BY o.created_at DESC LIMIT 1"
            ") FROM positions p"
        )
        if agent is None:
            rows = self.db.execute(query).fetchall()
        else:
            rows = self.db.execute(query + " WHERE p.agent=?", (agent,)).fetchall()
        return [dict(zip(names, row)) for row in rows]

    def closed_positions_for_regrowth(self, agent: str, *, max_age_seconds: float,
                                      now: float | None = None) -> list[dict]:
        """Mints this agent has fully exited recently enough to still be
        worth watching for renewed growth, newest exit first. Bounded by
        age so a mint that never recovers isn't polled forever."""
        at = time.time() if now is None else now
        rows = self.db.execute(
            "SELECT mint,exit_price,closed_at FROM closed_positions "
            "WHERE agent=? AND closed_at >= ? ORDER BY closed_at DESC",
            (agent, at - max_age_seconds),
        ).fetchall()
        return [dict(zip(("mint", "exit_price", "closed_at"), row)) for row in rows]

    def mark_position(self, *, agent: str, mint: str, price: float) -> dict:
        if not math.isfinite(price) or price <= 0:
            raise ValueError("position mark requires a positive finite price")
        self.db.execute("UPDATE positions SET peak_price=MAX(peak_price,?),current_price=?,updated_at=? "
                        "WHERE agent=? AND mint=?", (price, price, time.time(), agent, mint))
        row = next((p for p in self.positions(agent) if p["mint"] == mint), None)
        if row is None:
            raise ValueError("position not found")
        return row

    def reconcile_external_reduction(self, *, agent: str, mint: str,
                                     wallet_quantity_raw: int, wallet_decimals: int) -> None:
        """The wallet holds less of this mint than the ledger tracks (or none
        at all) - typically an operator selling manually outside the trial,
        same as observed live. No sell was executed on this ledger's own
        behalf, so there's no proceeds/realized P&L to record; this only
        brings the tracked position back in line with on-chain reality
        (or drops it, if nothing remains) so the trial can keep running
        instead of halting over someone else's trade."""
        if wallet_quantity_raw < 0:
            raise ValueError("wallet quantity cannot be negative")
        self._begin()
        try:
            row = self.db.execute(
                "SELECT quantity_raw FROM positions WHERE agent=? AND mint=?", (agent, mint)
            ).fetchone()
            if row is None:
                raise ValueError("position not found")
            if wallet_quantity_raw >= row[0]:
                raise ValueError("reconciliation requires a genuine reduction in wallet balance")
            if wallet_quantity_raw == 0:
                self.db.execute("DELETE FROM positions WHERE agent=? AND mint=?", (agent, mint))
            else:
                self.db.execute(
                    "UPDATE positions SET quantity_raw=?,decimals=?,updated_at=? "
                    "WHERE agent=? AND mint=?",
                    (wallet_quantity_raw, wallet_decimals, time.time(), agent, mint),
                )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def confirm_sell(self, *, intent: str, signature: str, quantity_raw: int,
                     proceeds_cents: int, verified_on_chain: bool) -> None:
        """Credit proceeds only after a matching on-chain token and USDC delta."""
        self._begin()
        try:
            order = self.db.execute("SELECT agent,mint,side,state,signature FROM orders WHERE intent=?", (intent,)).fetchone()
            if (not order or order[2] != "SELL" or order[3] not in {"SUBMITTED", "UNCERTAIN"}
                or order[4] != signature or verified_on_chain is not True
                or type(quantity_raw) is not int or quantity_raw <= 0
                or type(proceeds_cents) is not int or proceeds_cents <= 0):
                raise ValueError("sell fill is not independently verified")
            pos = self.db.execute("SELECT quantity_raw,cost_cents,decimals FROM positions WHERE agent=? AND mint=?", order[:2]).fetchone()
            realized = None
            if pos:
                if quantity_raw > pos[0]:
                    raise ValueError("sell quantity exceeds tracked position")
                remaining = pos[0] - quantity_raw
                cost_sold = pos[1] - round(pos[1] * remaining / pos[0])
                realized = proceeds_cents - cost_sold
                if remaining:
                    cost_remaining = round(pos[1] * remaining / pos[0])
                    self.db.execute("UPDATE positions SET quantity_raw=?,cost_cents=?,updated_at=?,"
                                    "principal_recovered=MAX(principal_recovered,?),"
                                    "second_stage_taken=MAX(second_stage_taken,?) "
                                    "WHERE agent=? AND mint=?", (
                                        remaining,cost_remaining,time.time(),
                                        int(intent.endswith(":PRINCIPAL")),
                                        int(intent.endswith(":SECOND_STAGE")), *order[:2],
                                    ))
                else:
                    self.db.execute("DELETE FROM positions WHERE agent=? AND mint=?", order[:2])
                    # A full exit's own fill price, watched afterward for
                    # renewed growth (see closed_positions_for_regrowth) -
                    # a mint hunter-v1 fully exits doesn't just vanish, in
                    # case it's still climbing. Only hunter-v1's own regrowth
                    # mechanism ever reads this table (see
                    # decide_regrowth_rebuy), so only its exits are worth
                    # recording here - a copy-v1 or portfolio-v1 exit would
                    # otherwise sit unused forever.
                    if order[0] == "hunter-v1":
                        exit_price = (proceeds_cents / 100) / (quantity_raw / 10 ** pos[2])
                        self.db.execute(
                            "INSERT INTO closed_positions(agent,mint,exit_price,closed_at) VALUES(?,?,?,?) "
                            "ON CONFLICT(agent,mint) DO UPDATE SET exit_price=excluded.exit_price,"
                            "closed_at=excluded.closed_at",
                            (order[0], order[1], exit_price, time.time()),
                        )
            self.db.execute("UPDATE orders SET state='CONFIRMED',proceeds_cents=?,realized_cents=? WHERE intent=?", (proceeds_cents,realized,intent))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def status(self, *, now: float | None = None) -> dict:
        row = self.db.execute("SELECT started_at,deadline,halted,reason FROM trial WHERE id=1").fetchone()
        if not row:
            return {"status": "NOT_STARTED", "agents": {}}
        at = time.time() if now is None else now
        agents = {}
        for agent in BUY_AGENTS:
            spent = self._spent(agent)
            proceeds = self.db.execute(
                "SELECT COALESCE(SUM(proceeds_cents),0) FROM orders "
                "WHERE agent=? AND side='SELL' AND state='CONFIRMED'", (agent,)
            ).fetchone()[0]
            agents[agent] = {
                "starting_budget_cents": AGENT_BUDGET_CENTS,
                "gross_buy_committed_cents": spent,
                "remaining_buy_cap_cents": AGENT_BUDGET_CENTS - spent,
                "confirmed_sell_proceeds_cents": int(proceeds),
                "available_cash_cents": AGENT_BUDGET_CENTS - spent + int(proceeds),
                "open_positions": len(self.positions(agent)),
                "position_cost_cents": sum(p["cost_cents"] for p in self.positions(agent)),
                "realized_pnl_cents": int(self.db.execute(
                    "SELECT COALESCE(SUM(realized_cents),0) FROM orders WHERE agent=? AND state='CONFIRMED'",
                    (agent,),
                ).fetchone()[0]),
                "unrealized_pnl_estimate_cents": sum(
                    round(p["quantity_raw"] * p["current_price"] / 10**p["decimals"] * 100)
                    - p["cost_cents"] for p in self.positions(agent)
                ),
            }
        return {
            "status": "HALTED" if row[2] else "EXPIRED" if at >= row[1] else "ACTIVE",
            "started_at": row[0], "deadline": row[1],
            "remaining_seconds": max(0, int(row[1] - at)), "halt_reason": row[3],
            "agents": agents,
            "total_remaining_buy_cap_cents": sum(a["remaining_buy_cap_cents"] for a in agents.values()),
            "total_gross_buy_committed_cents": sum(a["gross_buy_committed_cents"] for a in agents.values()),
            "model_requests": self.model_request_count(),
            "unresolved_orders": self.unresolved(),
            "recent_decisions": [
                {"at": at, "agent": agent, "mint": mint, "state": state, "reason": reason}
                for at, agent, mint, state, reason in self.db.execute(
                    "SELECT at,agent,mint,state,reason FROM decisions ORDER BY id DESC LIMIT 10"
                )
            ],
            "positions": self.positions(),
        }

    def report(self, *, now: float | None = None) -> dict:
        """Report confirmed fills; leave unknown fees and portfolio basis null."""
        status = self.status(now=now)
        if status["status"] == "NOT_STARTED":
            return status
        agents = {}
        for agent in (*BUY_AGENTS, "portfolio-v1"):
            orders = self.db.execute(
                "SELECT side,state,realized_cents FROM orders WHERE agent=? ORDER BY created_at,intent",
                (agent,),
            ).fetchall()
            realized = [row[2] for row in orders if row[0] == "SELL" and row[1] == "CONFIRMED" and row[2] is not None]
            wins = [n for n in realized if n > 0]
            losses = [n for n in realized if n < 0]
            total_profit = sum(wins)
            total_loss = -sum(losses)
            running = peak = worst = 0
            for value in realized:
                running += value
                peak = max(peak, running)
                worst = max(worst, peak - running)
            marks = self.positions(agent)
            agents[agent] = {
                "starting_budget_cents": AGENT_BUDGET_CENTS if agent in BUY_AGENTS else 0,
                "gross_buy_committed_cents": self._spent(agent),
                "available_cash_estimate_cents": (
                    AGENT_BUDGET_CENTS - self._spent(agent) +
                    int(self.db.execute(
                        "SELECT COALESCE(SUM(proceeds_cents),0) FROM orders WHERE agent=? "
                        "AND side='SELL' AND state='CONFIRMED'", (agent,),
                    ).fetchone()[0])
                ) if agent in BUY_AGENTS else None,
                "confirmed_buys": sum(row[0] == "BUY" and row[1] == "CONFIRMED" for row in orders),
                "confirmed_sells": sum(row[0] == "SELL" and row[1] == "CONFIRMED" for row in orders),
                "failed_orders": sum(row[1] == "FAILED" for row in orders),
                "open_positions": len(marks),
                "marked_open_value_cents": sum(round(p["current_price"] * p["quantity_raw"]
                                          / 10**p["decimals"] * 100) for p in marks),
                "oldest_open_mark_epoch": min((p["updated_at"] for p in marks), default=None),
                "realized_pnl_cents": sum(realized) if agent in BUY_AGENTS else None,
                "winning_trades_with_known_basis": len(wins),
                "losing_trades_with_known_basis": len(losses),
                "win_rate_known_basis": len(wins) / len(realized) if realized else None,
                "average_win_cents": total_profit / len(wins) if wins else None,
                "average_loss_cents": total_loss / len(losses) if losses else None,
                "largest_win_cents": max(wins, default=None),
                "largest_loss_cents": min(losses, default=None),
                "max_realized_drawdown_cents": worst,
                "profit_factor": total_profit / total_loss if total_loss else None,
                "expectancy_cents": sum(realized) / len(realized) if realized else None,
                "fees_usd": None,
                "estimated_slippage_usd": None,
            }
        return {
            "status": status["status"], "started_at": status["started_at"],
            "deadline": status["deadline"], "generated_at": time.time(),
            "max_new_capital_cents": 6000,
            "agents": agents,
            "model_performance_by_decision": self.model_performance_by_decision(),
            "blocked_decisions": self.db.execute(
                "SELECT COUNT(*) FROM decisions WHERE state IN ('BLOCKED','EXIT_BLOCKED','CYCLE_BLOCKED')"
            ).fetchone()[0],
            "unresolved_orders": status["unresolved_orders"],
            "note": "Open values use last observed marks and can be stale; unknown fees, slippage and portfolio cost basis are not estimated.",
        }

    def model_performance_by_decision(self, agent: str = "hunter-v1") -> dict[str, dict[str, Any]]:
        """Realized P&L grouped by the recommendation-engine decision label
        (MOMENTUM BUY, BUY NOW, BUY ZONE, EARLY BUY, REGROWTH REBUY) that
        sourced each buy - the model must approve every one of these before
        a buy executes, so this answers "is the model's approval actually
        associated with profit, or just adding latency" per decision type,
        not just in aggregate. Walks orders chronologically per mint,
        attributing each sell's realized P&L to the most recent preceding
        buy's decision label for that mint - handles a mint being bought,
        sold, and rebought under a different decision type within one
        session.
        """
        rows = self.db.execute(
            "SELECT mint, side, decision, realized_cents FROM orders "
            "WHERE agent=? AND state='CONFIRMED' ORDER BY created_at",
            (agent,),
        ).fetchall()
        current_decision: dict[str, str] = {}
        by_decision: dict[str, dict[str, Any]] = {}
        for mint, side, decision, realized_cents in rows:
            if side == "BUY":
                current_decision[mint] = decision or "UNKNOWN"
            elif side == "SELL" and realized_cents is not None:
                label = current_decision.get(mint, "UNKNOWN")
                bucket = by_decision.setdefault(
                    label, {"round_trips": 0, "wins": 0, "losses": 0, "realized_cents": 0}
                )
                bucket["round_trips"] += 1
                bucket["realized_cents"] += realized_cents
                if realized_cents > 0:
                    bucket["wins"] += 1
                elif realized_cents < 0:
                    bucket["losses"] += 1
        return by_decision
