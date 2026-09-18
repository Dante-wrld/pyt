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

from .portfolio import PortfolioSignal
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
        priority: int = 0,
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
        priority: int = 0,
    ) -> str | None:
        if priority not in {-2, -1, 0, 1}:
            raise ValueError("Pushover priority must be -2 through 1")
        payload: dict[str, Any] = {
            "token": self.app_token,
            "user": self.user_key,
            "title": title,
            "message": message,
            "priority": priority,
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
                "User-Agent": "solana-launch-guard/0.15",
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
        high_priority_decisions: tuple[str, ...] = ("BUY NOW", "BUY ZONE"),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client = client
        self.store = store
        self.decisions = frozenset(decisions)
        self.min_score = min_score
        self.cooldown_seconds = cooldown_seconds
        self.high_priority_decisions = frozenset(high_priority_decisions)
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
                priority=(
                    1 if candidate.decision in self.high_priority_decisions else 0
                ),
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
                "Phone notifications are connected. Automated selling stays "
                "off unless it is explicitly enabled and a token is armed."
            ),
            sound="magic",
        )

    async def send_high_priority_test(self) -> str | None:
        return await self.client.send(
            title="🔴 Launch Guard high-priority test",
            message=(
                "High-priority buy/sell alerts are connected. This is only "
                "a notification test; no order was placed."
            ),
            sound="siren",
            priority=1,
        )


class PortfolioNotifier:
    """Send state-change alerts for read-only owned-holding guidance."""

    def __init__(
        self,
        *,
        client: PushClient,
        store: NotificationStore,
        decisions: tuple[str, ...],
        high_priority_decisions: tuple[str, ...],
        cooldown_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client = client
        self.store = store
        self.decisions = frozenset(decisions)
        self.high_priority_decisions = frozenset(high_priority_decisions)
        self.cooldown_seconds = cooldown_seconds
        self.clock = clock
        self._retry_after: dict[str, float] = {}

    async def maybe_send(self, signal: PortfolioSignal) -> bool:
        key = f"{signal.chain}:{signal.token_address.casefold()}"
        previous_state = self.store.last_notification(
            "portfolio_signal", key
        )
        if previous_state is not None and previous_state[0] == signal.decision:
            return False

        now = self.clock()
        if signal.decision not in self.decisions:
            self._save_state(signal, key, now)
            return False
        if now < self._retry_after.get(key, 0):
            return False

        previous_alert = self.store.last_notification(
            "pushover_portfolio", key
        )
        if (
            previous_alert is not None
            and now - previous_alert[1] < self.cooldown_seconds
        ):
            return False

        title, message, sound = format_portfolio_notification(signal)
        try:
            request_id = await self.client.send(
                title=title,
                message=message,
                url=(
                    f"https://dexscreener.com/{signal.chain}/"
                    f"{signal.token_address}"
                ),
                url_title="Open DEX Screener",
                sound=sound,
                priority=(
                    1
                    if signal.decision in self.high_priority_decisions
                    else 0
                ),
            )
        except ConnectionError:
            self._retry_after[key] = now + 60
            raise

        self.store.save_notification(
            provider="pushover_portfolio",
            candidate_key=key,
            symbol=signal.symbol,
            decision=signal.decision,
            sent_at=now,
            request_id=request_id,
        )
        self._save_state(signal, key, now)
        return True

    def _save_state(
        self, signal: PortfolioSignal, key: str, now: float
    ) -> None:
        self.store.save_notification(
            provider="portfolio_signal",
            candidate_key=key,
            symbol=signal.symbol,
            decision=signal.decision,
            sent_at=now,
            request_id=None,
        )


def format_portfolio_notification(
    signal: PortfolioSignal,
) -> tuple[str, str, str]:
    presentation = {
        "EXIT WARNING": ("🔴", "REVIEW SELL", "siren"),
        "PROTECT PROFIT": ("🟠", "PROTECT PROFIT", "spacealarm"),
        "TAKE PARTIAL": ("🟡", "CONSIDER PARTIAL PROFIT", "cashregister"),
    }
    icon, label, sound = presentation.get(
        signal.decision, ("⚡", signal.decision, "pushover")
    )
    prefix = "$" if signal.price_currency == "USD" else ""
    suffix = " SOL" if signal.price_currency == "SOL" else ""
    price = (
        f"{prefix}{signal.current_price:.12g}{suffix}"
        if signal.current_price is not None
        else "unavailable"
    )
    pnl = f"{signal.pnl_pct:+.2f}%" if signal.pnl_pct is not None else "n/a"
    value = (
        f"${signal.current_value_usd:,.2f}"
        if signal.current_value_usd is not None
        else "n/a"
    )
    momentum = (
        f"{signal.price_change_m5_pct:+.2f}%"
        if signal.price_change_m5_pct is not None
        else "n/a"
    )
    message = "\n".join(
        (
            f"TOKEN: {signal.symbol} • {signal.chain.upper()}",
            f"Status: {signal.decision}",
            f"Current: {price}",
            f"Position value: {value}",
            f"P/L: {pnl}",
            f"5m move: {momentum}",
            f"Buy/Sell count: {signal.buys_m5}/{signal.sells_m5}",
            f"Reason: {signal.reason}",
            "Advisory only — no order was placed.",
        )
    )
    return f"{icon} Launch Guard: {label}", message, sound


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
