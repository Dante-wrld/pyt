"""Read-only, restart-safe history for recommendation pullbacks."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .recommendations import RecommendationBook, RecommendationCandidate


class PullbackTracker:
    def __init__(self, snapshot_path: str | Path) -> None:
        path = Path(snapshot_path).expanduser()
        self.path = path.with_name(path.name + ".pullbacks.json")
        self.history_path = path.with_name(path.name + ".pullbacks.jsonl")
        self.previous: dict[str, str] = {}

    def restore(self, book: RecommendationBook, *, now: float | None = None) -> int:
        """Resume only fresh candidates; never turn an old quote into a new signal."""
        current = time.time() if now is None else now
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0
        if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
            raise ValueError("invalid pullback tracking state")
        restored = 0
        for raw in payload["candidates"]:
            try:
                candidate = RecommendationCandidate.from_json(json.dumps(raw))
            except (TypeError, ValueError, KeyError):
                continue
            if (
                candidate.entry_zone_low is None
                or candidate.entry_zone_high is None
                or candidate.updated_at > current
                or current - candidate.updated_at > book.ttl_seconds
            ):
                continue
            if candidate.key in book.candidates:
                continue
            restored += int(book.restore(candidate))
        self.previous = {
            c.key: self._state(c) for c in book.candidates.values()
            if c.entry_zone_low is not None and c.entry_zone_high is not None
        }
        return restored

    @staticmethod
    def _state(candidate: RecommendationCandidate) -> str:
        if candidate.decision in {"BUY NOW", "BUY ZONE", "MOMENTUM BUY"}:
            return "confirmed_entry"
        if candidate.decision == "ENTRY PENDING":
            return "confirmation_pending"
        if candidate.decision == "AVOID":
            return "risk_blocked"
        # Callers only reach here for candidates with both zone bounds set
        # (see the filter in restore()).
        assert candidate.entry_zone_low is not None
        assert candidate.entry_zone_high is not None
        if candidate.current_price < candidate.entry_zone_low:
            return "fell_through_zone"
        if candidate.current_price <= candidate.entry_zone_high:
            return "in_zone_unconfirmed"
        if candidate.decision == "PULLBACK STARTED":
            return "pullback_started"
        return "above_zone"

    def record(self, book: RecommendationBook, *, now: float | None = None) -> None:
        current = time.time() if now is None else now
        active = {
            c.key: c for c in book.candidates.values()
            if c.entry_zone_low is not None and c.entry_zone_high is not None
        }
        states = {key: self._state(c) for key, c in active.items()}
        events = []
        for key, candidate in active.items():
            state = states[key]
            if self.previous.get(key) == state:
                continue
            events.append({
                "at": current, "chain": candidate.chain, "mint": candidate.mint,
                "symbol": candidate.symbol, "state": state,
                "price": candidate.current_price,
                "entry_zone_low": candidate.entry_zone_low,
                "entry_zone_high": candidate.entry_zone_high,
                "decision": candidate.decision,
                "reason": candidate.decision_reason,
                "confirmations": candidate.entry_confirmation_count,
                "confirmation_required": candidate.entry_confirmation_required,
            })
        for key in self.previous.keys() - states.keys():
            chain, mint = key.split(":", 1)
            events.append({"at": current, "chain": chain, "mint": mint,
                           "state": "left_tracking_pool", "reason": "expired or displaced; subsequent outcome unknown"})
        # Save state before emitting events, so a failed write never claims that
        # an unpersisted set of watches will survive a restart.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps({"version": 1, "saved_at": current,
            "candidates": [json.loads(c.to_json()) for c in active.values()]},
            separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, self.path)
        if events:
            with self.history_path.open("a", encoding="utf-8") as stream:
                for event in events:
                    stream.write(json.dumps(event, separators=(",", ":")) + "\n")
        self.previous = states
