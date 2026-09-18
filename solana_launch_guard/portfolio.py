from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .market import MarketQuote


@dataclass(frozen=True, slots=True)
class OwnedHolding:
    chain: str
    token_address: str
    symbol: str
    quantity: float
    entry_price: float | None = None
    price_currency: str | None = None
    cost_amount: float | None = None


@dataclass(slots=True)
class PortfolioSignal:
    chain: str
    token_address: str
    symbol: str
    quantity: float
    current_price: float | None
    price_currency: str
    current_value_usd: float | None
    pnl_pct: float | None
    decision: str
    reason: str
    price_change_m5_pct: float | None
    buys_m5: int
    sells_m5: int
    liquidity_usd: float | None
    entry_price: float | None
    peak_price: float | None


@dataclass(slots=True)
class _HoldingState:
    peak_price: float
    baseline_liquidity_usd: float


class PortfolioAdvisor:
    """Stateful, read-only exit guidance for wallet holdings."""

    def __init__(
        self,
        *,
        take_partial_pct: float = 30,
        stop_loss_pct: float = 20,
        trailing_activation_pct: float = 20,
        trailing_stop_pct: float = 12,
        momentum_exit_pct: float = -8,
        sell_pressure_ratio: float = 1.5,
        liquidity_drop_pct: float = 30,
    ) -> None:
        self.take_partial_pct = take_partial_pct
        self.stop_loss_pct = stop_loss_pct
        self.trailing_activation_pct = trailing_activation_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.momentum_exit_pct = momentum_exit_pct
        self.sell_pressure_ratio = sell_pressure_ratio
        self.liquidity_drop_pct = liquidity_drop_pct
        self.states: dict[str, _HoldingState] = {}

    def restore_state(
        self,
        *,
        chain: str,
        token_address: str,
        peak_price: float,
        baseline_liquidity_usd: float,
    ) -> None:
        if peak_price <= 0:
            return
        key = f"{chain}:{token_address.casefold()}"
        self.states[key] = _HoldingState(
            peak_price=peak_price,
            baseline_liquidity_usd=max(0, baseline_liquidity_usd),
        )

    def state_for(
        self, chain: str, token_address: str
    ) -> tuple[float, float] | None:
        state = self.states.get(f"{chain}:{token_address.casefold()}")
        if state is None:
            return None
        return state.peak_price, state.baseline_liquidity_usd

    def evaluate(
        self,
        holding: OwnedHolding,
        quote: MarketQuote | None,
        *,
        sol_usd: float | None = None,
    ) -> PortfolioSignal:
        if quote is None:
            return PortfolioSignal(
                chain=holding.chain,
                token_address=holding.token_address,
                symbol=holding.symbol,
                quantity=holding.quantity,
                current_price=None,
                price_currency=holding.price_currency or "UNKNOWN",
                current_value_usd=None,
                pnl_pct=None,
                decision="UNPRICED",
                reason="market quote unavailable; no sell inference made",
                price_change_m5_pct=None,
                buys_m5=0,
                sells_m5=0,
                liquidity_usd=None,
                entry_price=holding.entry_price,
                peak_price=None,
            )

        if holding.price_currency == "SOL" and quote.price_sol > 0:
            price = quote.price_sol
            currency = "SOL"
        else:
            price = quote.recommendation_price
            currency = quote.recommendation_currency
        key = f"{holding.chain}:{holding.token_address.casefold()}"
        state = self.states.get(key)
        if state is None:
            state = _HoldingState(
                peak_price=price,
                baseline_liquidity_usd=quote.liquidity_usd or 0,
            )
            self.states[key] = state
        state.peak_price = max(state.peak_price, price)

        entry_price = (
            holding.entry_price
            if holding.price_currency == currency
            else None
        )
        pnl_pct = (
            (price / entry_price - 1) * 100
            if entry_price is not None and entry_price > 0
            else None
        )
        current_value_usd = (
            holding.quantity * quote.price_usd
            if quote.price_usd is not None
            else (
                holding.quantity * quote.price_sol * sol_usd
                if sol_usd is not None
                else None
            )
        )
        sell_pressure = quote.sells_m5 / max(1, quote.buys_m5)
        momentum_reversal = (
            quote.price_change_m5_pct is not None
            and quote.price_change_m5_pct <= self.momentum_exit_pct
            and sell_pressure >= self.sell_pressure_ratio
            and quote.sells_m5 >= 5
        )
        liquidity_floor = state.baseline_liquidity_usd * (
            1 - self.liquidity_drop_pct / 100
        )
        liquidity_break = (
            state.baseline_liquidity_usd > 0
            and (quote.liquidity_usd or 0) < liquidity_floor
            and quote.sells_m5 > quote.buys_m5
        )
        drawdown_from_peak = (price / state.peak_price - 1) * 100

        if pnl_pct is not None and pnl_pct <= -self.stop_loss_pct:
            decision = "EXIT WARNING"
            reason = (
                f"cost-basis loss {pnl_pct:.1f}% reached the "
                f"-{self.stop_loss_pct:.1f}% risk limit"
            )
        elif momentum_reversal:
            decision = "EXIT WARNING"
            reason = (
                f"5m momentum {quote.price_change_m5_pct:.1f}% with "
                f"seller/buyer pressure {sell_pressure:.2f}x"
            )
        elif liquidity_break:
            liquidity_change = (
                ((quote.liquidity_usd or 0) / state.baseline_liquidity_usd - 1)
                * 100
            )
            decision = "EXIT WARNING"
            reason = f"liquidity fell {abs(liquidity_change):.1f}% with net selling"
        elif (
            pnl_pct is not None
            and pnl_pct >= self.trailing_activation_pct
            and drawdown_from_peak <= -self.trailing_stop_pct
        ):
            decision = "PROTECT PROFIT"
            reason = (
                f"price is {abs(drawdown_from_peak):.1f}% below its monitored "
                f"peak; open gain is {pnl_pct:.1f}%"
            )
        elif pnl_pct is not None and pnl_pct >= self.take_partial_pct:
            decision = "TAKE PARTIAL"
            reason = (
                f"open gain {pnl_pct:.1f}% reached the "
                f"{self.take_partial_pct:.1f}% partial-profit level"
            )
        else:
            decision = "HOLD"
            if pnl_pct is None:
                reason = "market structure is inside limits; cost basis unknown"
            else:
                reason = f"open P/L {pnl_pct:+.1f}%; risk triggers not reached"

        return PortfolioSignal(
            chain=holding.chain,
            token_address=holding.token_address,
            symbol=quote.symbol or holding.symbol,
            quantity=holding.quantity,
            current_price=price,
            price_currency=currency,
            current_value_usd=current_value_usd,
            pnl_pct=pnl_pct,
            decision=decision,
            reason=reason,
            price_change_m5_pct=quote.price_change_m5_pct,
            buys_m5=quote.buys_m5,
            sells_m5=quote.sells_m5,
            liquidity_usd=quote.liquidity_usd,
            entry_price=entry_price,
            peak_price=state.peak_price,
        )


def build_portfolio_snapshot(
    signals: list[PortfolioSignal],
    *,
    wallet: str | None,
    poll_seconds: float,
    execution_mode: str = "read-only",
) -> dict[str, Any]:
    priority = {
        "EXIT WARNING": 4,
        "PROTECT PROFIT": 3,
        "TAKE PARTIAL": 2,
        "HOLD": 1,
        "UNPRICED": 0,
    }
    ordered = sorted(
        signals,
        key=lambda item: (
            priority.get(item.decision, 0),
            item.current_value_usd or 0,
        ),
        reverse=True,
    )
    return {
        "generated_at": time.time(),
        "wallet": wallet,
        "poll_seconds": poll_seconds,
        "execution_mode": execution_mode,
        "signals": [asdict(item) for item in ordered],
    }


def write_portfolio_snapshot(path: str | Path, snapshot: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    os.replace(temporary, destination)


def read_portfolio_snapshot(path: str | Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def format_portfolio_dashboard(
    snapshot: dict[str, Any], *, color: bool = True
) -> str:
    reset = "\033[0m" if color else ""
    bold = "\033[1m" if color else ""
    colors = {
        "EXIT WARNING": "\033[38;5;196m" if color else "",
        "PROTECT PROFIT": "\033[38;5;208m" if color else "",
        "TAKE PARTIAL": "\033[38;5;220m" if color else "",
        "HOLD": "\033[38;5;46m" if color else "",
        "UNPRICED": "\033[38;5;244m" if color else "",
    }
    generated = float(snapshot.get("generated_at") or 0)
    updated = datetime.fromtimestamp(
        generated, tz=timezone.utc
    ).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    wallet = str(snapshot.get("wallet") or "manual/imported holdings")
    rows = snapshot.get("signals") or []
    execution_mode = str(snapshot.get("execution_mode") or "read-only")
    mode_label = {
        "live": "AUTO-SELL LIVE",
        "dry-run": "AUTO-SELL DRY RUN",
    }.get(execution_mode, "READ-ONLY")
    lines = [
        f"{bold}LAUNCH GUARD — MY HOLDINGS ({mode_label}){reset}",
        f"Wallet: {wallet}",
        f"Updated: {updated} | Holdings: {len(rows)}",
        (
            "Only armed 2x/3x profit-ladder events can submit a USDC sell."
            if execution_mode == "live"
            else "Signals are advisory. No order can be signed or submitted."
        ),
        "",
    ]
    if not rows:
        lines.append("No non-zero priced token holdings found yet.")
        return "\n".join(lines)

    for index, raw in enumerate(rows, start=1):
        decision = str(raw.get("decision") or "UNPRICED")
        shade = colors.get(decision, "")
        symbol = str(raw.get("symbol") or "UNKNOWN")
        currency = str(raw.get("price_currency") or "")
        current = raw.get("current_price")
        prefix = "$" if currency == "USD" else ""
        suffix = " SOL" if currency == "SOL" else ""
        price_text = (
            f"{prefix}{float(current):.12g}{suffix}"
            if current is not None
            else "unavailable"
        )
        pnl = raw.get("pnl_pct")
        pnl_text = f"{float(pnl):+.2f}%" if pnl is not None else "n/a"
        value = raw.get("current_value_usd")
        value_text = f"${float(value):,.2f}" if value is not None else "n/a"
        change = raw.get("price_change_m5_pct")
        change_text = f"{float(change):+.2f}%" if change is not None else "n/a"
        lines.extend(
            [
                f"{shade}{bold}#{index:02d} {symbol:<12} {decision}{reset}",
                f"{shade}    quantity={float(raw.get('quantity') or 0):.8g} "
                f"value={value_text} price={price_text} P/L={pnl_text}{reset}",
                f"{shade}    m5={change_text} buys/sells="
                f"{int(raw.get('buys_m5') or 0)}/"
                f"{int(raw.get('sells_m5') or 0)} "
                f"liquidity=${float(raw.get('liquidity_usd') or 0):,.0f}{reset}",
                f"{shade}    reason={raw.get('reason') or ''}{reset}",
                f"{shade}    token={raw.get('token_address') or ''}{reset}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()
