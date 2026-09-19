from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class LearningSource:
    source_id: str
    title: str
    author: str
    url: str
    published_at: str | None
    transcript: str
    added_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_transcript(
        cls,
        *,
        title: str,
        author: str,
        url: str,
        transcript: str,
        published_at: str | None = None,
    ) -> "LearningSource":
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("an attributable HTTPS source URL is required")
        if len(transcript.strip()) < 100:
            raise ValueError("transcript is too short for strategy research")
        digest = hashlib.sha256((url + transcript).encode()).hexdigest()[:16]
        return cls(digest, title.strip(), author.strip(), url, published_at, transcript.strip())


@dataclass(frozen=True, slots=True)
class StrategyLesson:
    lesson_id: str
    source_id: str
    claim: str
    transcript_excerpt: str
    entry_rule: str
    exit_rule: str
    invalidation_rule: str
    risk_rule: str
    status: str = "RESEARCH_ONLY"

    def validate(self, source: LearningSource) -> None:
        if self.source_id != source.source_id:
            raise ValueError("lesson source does not match")
        if self.transcript_excerpt not in source.transcript:
            raise ValueError("lesson excerpt is not present in the attributed transcript")
        if self.status != "RESEARCH_ONLY":
            raise ValueError("video-derived lessons must begin in RESEARCH_ONLY status")
        for value in (
            self.claim,
            self.entry_rule,
            self.exit_rule,
            self.invalidation_rule,
            self.risk_rule,
        ):
            if not value.strip():
                raise ValueError("lessons require complete, testable rules")


@dataclass(frozen=True, slots=True)
class PromotionEvidence:
    sample_trades: int
    net_return_pct: float
    max_drawdown_pct: float
    forward_test_days: int
    fees_and_slippage_included: bool


@dataclass(frozen=True, slots=True)
class LearningPolicy:
    minimum_sample_trades: int = 50
    minimum_forward_test_days: int = 30
    maximum_drawdown_pct: float = 12
    minimum_net_return_pct: float = 0

    def may_promote(self, evidence: PromotionEvidence) -> tuple[bool, tuple[str, ...]]:
        failures: list[str] = []
        if evidence.sample_trades < self.minimum_sample_trades:
            failures.append("insufficient paper-trade sample")
        if evidence.forward_test_days < self.minimum_forward_test_days:
            failures.append("forward-test window is too short")
        if evidence.max_drawdown_pct > self.maximum_drawdown_pct:
            failures.append("maximum drawdown exceeds the learning limit")
        if evidence.net_return_pct <= self.minimum_net_return_pct:
            failures.append("net return is not positive")
        if not evidence.fees_and_slippage_included:
            failures.append("fees and slippage were not included")
        return not failures, tuple(failures or ["paper evidence passed promotion gates"])
