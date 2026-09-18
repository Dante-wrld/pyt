from __future__ import annotations

import asyncio
import json
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

import certifi

from .recommendations import CHAIN_LABELS, RecommendationCandidate

PUSHOVER_MESSAGES_URL = "https://api.pushover.net/1/messages.json"


class NotificationStore(Protocol):
    def last_notification(
        self, provider: str, candidate_key: str
    ) -> tuple[str, float] | None: ...

    def save_notification(
        self,
        *,
        provider: str,
        candidate_key: str,
        symbol: str,
        decision: str,
        sent_at: float,
        request_id: str | None,
    ) -> None: ...


class PushClient(Protocol):
    async def send(
        self,
        *,
        title: str,
        message: str,
        url: str | None = None,
        url_title: str | None = None,
        sound: str | None = None,
    ) -> str | None: ...


class PushoverClient:
    def __init__(
        self,
        *,
        app_token: str,
        user_key: str,
        device: str | None = None,
    ) -> None:
        self.app_token = app_token
        self.user_key = user_key
        self.device = device
        self._ssl = ssl.create_default_context(cafile=certifi.where())

    async def send(
        self,
        *,
        title: str,
        message: str,
        url: str | None = None,
        url_title: str | None = None,
        sound: str | None = None,
    ) -> str | None:
        payload: dict[str, Any] = {
            "token": self.app_token,
            "user": self.user_key,
            "title": title,
            "message": message,
            "priority": 0,
            "ttl": 900,
        }
        if self.device:
            payload["device"] = self.device
        if url:
            payload["url"] = url
        if url_title:
            payload["url_title"] = url_title
        if sound:
            payload["sound"] = sound
        return await asyncio.to_thread(self._post, payload)

    def _post(self, payload: dict[str, Any]) -> str | None:
        request = urllib.request.Request(
            PUSHOVER_MESSAGES_URL,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "solana-launch-guard/0.11",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=10, context=self._ssl
            ) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            raise ConnectionError(
                f"Pushover rejected the notification (HTTP {exc.code})"
            ) from exc
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise ConnectionError(f"Pushover request failed: {exc}") from exc
        if not isinstance(result, dict) or int(result.get("status") or 0) != 1:
            raise ConnectionError("Pushover returned an unsuccessful response")
        request_id = result.get("request")
        return str(request_id) if request_id else None


class DecisionNotifier:
    def __init__(
        self,
        *,
        client: PushClient,
        store: NotificationStore,
        decisions: tuple[str, ...],
        min_score: int,
        cooldown_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client = client
        self.store = store
        self.decisions = frozenset(decisions)
        self.min_score = min_score
        self.cooldown_seconds = cooldown_seconds
        self.clock = clock
        self._retry_after: dict[str, float] = {}

    async def maybe_send(self, candidate: RecommendationCandidate) -> bool:
        if (
            candidate.decision not in self.decisions
            or (
                candidate.decision != "AVOID"
                and candidate.signal_score < self.min_score
            )
        ):
            return False

        now = self.clock()
        if now < self._retry_after.get(candidate.key, 0):
            return False
        previous = self.store.last_notification("pushover", candidate.key)
        if previous is not None:
            previous_decision, previous_sent_at = previous
            if previous_decision == candidate.decision:
                return False
            if (
                candidate.decision
                not in {"PULLBACK STARTED", "BUY ZONE", "AVOID"}
                and now - previous_sent_at < self.cooldown_seconds
            ):
                return False

        title, message, sound = format_candidate_notification(candidate)
        try:
            request_id = await self.client.send(
                title=title,
                message=message,
                url=candidate.market_url,
                url_title="Open DEX Screener",
                sound=sound,
            )
        except ConnectionError:
            self._retry_after[candidate.key] = now + 60
            raise

        self.store.save_notification(
            provider="pushover",
            candidate_key=candidate.key,
            symbol=candidate.symbol,
            decision=candidate.decision,
            sent_at=now,
            request_id=request_id,
        )
        return True

    async def send_test(self) -> str | None:
        return await self.client.send(
            title="Launch Guard test",
            message=(
                "Phone notifications are connected. Launch Guard remains "
                "read-only and will never place a trade."
            ),
            sound="magic",
        )


def format_candidate_notification(
    candidate: RecommendationCandidate,
) -> tuple[str, str, str]:
    presentation = {
        "BUY NOW": ("🟢", "QUALIFIED ENTRY", "magic"),
        "BUY ZONE": ("🟢", "ENTRY ZONE REACHED", "cashregister"),
        "ENTRY PENDING": ("🟡", "ENTRY CONFIRMING", "pushover"),
        "PULLBACK STARTED": ("🔵", "PULLBACK STARTED", "siren"),
        "WAIT FOR PULLBACK": ("🔵", "WAIT FOR PULLBACK", "pushover"),
        "WATCH": ("🟡", "WATCH", "pushover"),
        "AVOID": ("🔴", "SETUP INVALIDATED", "falling"),
    }
    icon, label, sound = presentation.get(
        candidate.decision, ("⚡", candidate.decision, "pushover")
    )
    chain = CHAIN_LABELS.get(candidate.chain, candidate.chain.upper()[:5])
    price_prefix = "$" if candidate.price_currency == "USD" else ""
    lines = [
        f"TOKEN: {candidate.symbol} • {chain}",
        f"Score: {candidate.signal_score}/100",
        "Entry confirmation: "
        f"{candidate.entry_confirmation_count}/"
        f"{candidate.entry_confirmation_required}",
        f"Current: {price_prefix}{candidate.current_price:.12g}",
    ]
    if candidate.entry_zone_low is not None and candidate.entry_zone_high is not None:
        lines.append(
            "Entry zone: "
            f"{price_prefix}{candidate.entry_zone_low:.12g}–"
            f"{price_prefix}{candidate.entry_zone_high:.12g}"
        )
    if candidate.planned_entry_price is not None:
        lines.append(
            "Paper risk plan: entry "
            f"{price_prefix}{candidate.planned_entry_price:.12g}, stop "
            f"{price_prefix}{(candidate.planned_stop_price or 0):.12g}, "
            f"target {price_prefix}{(candidate.planned_target_price or 0):.12g} "
            f"({candidate.planned_reward_risk_ratio:.2f}R)"
        )
    lines.extend(
        (
            f"Momentum: {candidate.momentum_label}",
            f"Liquidity: {candidate.liquidity_label}",
            f"Volume: {candidate.volume_label}",
            f"Risk: {candidate.risk_label}",
            f"Status: {candidate.decision}",
            f"Reason: {candidate.decision_reason}",
        )
    )
    return f"{icon} Launch Guard: {label}", "\n".join(lines), sound
