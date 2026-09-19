from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LIVE_CONFIRMATION = "SPEND_1_USDC_ON_MAINNET"
USDC_DECIMALS = 6


def _enabled(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class CanaryPolicy:
    amount_usd: float = 1.0
    minimum_liquidity_usd: float = 50_000.0
    maximum_signal_age_seconds: float = 15.0
    maximum_price_impact_pct: float = 3.0
    maximum_slippage_bps: int = 300

    @classmethod
    def from_env(cls) -> "CanaryPolicy":
        amount = float(os.getenv("AGENT_LIVE_TEST_AMOUNT_USD", "1"))
        if not math.isclose(amount, 1.0):
            raise ValueError("the first live canary is hard-limited to exactly $1")
        return cls(amount_usd=amount)


def select_live_candidate(snapshot: dict[str, Any], policy: CanaryPolicy) -> dict[str, Any]:
    generated_at = float(snapshot.get("generated_at") or 0)
    age = time.time() - generated_at
    if generated_at <= 0 or age < 0 or age > policy.maximum_signal_age_seconds:
        raise ValueError("recommendation snapshot is missing or stale")
    candidates = snapshot.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("recommendation snapshot has no candidates")
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        if raw.get("chain") not in {None, "solana"}:
            continue
        if raw.get("decision") not in {"BUY NOW", "BUY ZONE"}:
            continue
        if float(raw.get("liquidity_usd") or 0) < policy.minimum_liquidity_usd:
            continue
        if int(raw.get("entry_confirmation_count") or 0) < int(
            raw.get("entry_confirmation_required") or 3
        ):
            continue
        mint = str(raw.get("mint") or "")
        if 32 <= len(mint) <= 44:
            return raw
    raise ValueError("no fresh, confirmed, sufficiently liquid Solana buy candidate")


class CanaryJournal:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "attempts": []}
        if not isinstance(value, dict) or not isinstance(value.get("attempts"), list):
            raise ValueError("live canary journal is invalid")
        return value

    def assert_unused(self) -> None:
        attempts = self.load()["attempts"]
        if attempts:
            raise ValueError(
                "the one-time live canary has already been attempted; inspect its journal"
            )

    def record(self, entry: dict[str, Any]) -> None:
        payload = self.load()
        payload["attempts"].append(entry)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


def validate_live_environment(*, execute: bool, confirmation: str | None) -> CanaryPolicy:
    policy = CanaryPolicy.from_env()
    if not _enabled("AGENT_LIVE_TEST_ENABLED"):
        raise ValueError("AGENT_LIVE_TEST_ENABLED is false")
    if _enabled("AGENT_LIVE_KILL_SWITCH", True):
        raise ValueError("AGENT_LIVE_KILL_SWITCH is active")
    required = ("SOLANA_WALLET_ADDRESS", "JUPITER_API_KEY")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise ValueError("missing live setting(s): " + ", ".join(missing))
    if not (_enabled("AUTO_SELL_ENABLED") and _enabled("AUTO_SELL_LIVE")):
        raise ValueError("live canary requires live exit protection")
    if execute and confirmation != LIVE_CONFIRMATION:
        raise ValueError(f"live execution requires --confirm {LIVE_CONFIRMATION}")
    return policy


async def run_live_canary(
    *, execute: bool, confirmation: str | None = None
) -> dict[str, Any]:
    policy = validate_live_environment(execute=execute, confirmation=confirmation)
    snapshot_path = Path(
        os.getenv("RECOMMENDATION_SNAPSHOT_PATH", "launch_guard_recommendations.json")
    )
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"could not read recommendation snapshot: {exc}") from exc
    candidate = select_live_candidate(snapshot, policy)
    mint = str(candidate["mint"])
    symbol = str(candidate.get("symbol") or mint[:8])
    journal = CanaryJournal(
        os.getenv("AGENT_LIVE_CANARY_PATH", "launch_guard_live_canary.json")
    )
    journal.assert_unused()

    # Lazy imports keep shadow-only commands independent from live dependencies.
    from .execution import (
        BuyIntent,
        JupiterExecutionError,
        JupiterSwapClient,
        KeyringSolanaSigner,
        SolanaAutoBuyer,
        USDC_MINT,
    )
    from .core import SQLiteStore
    from .portfolio import OwnedHolding
    from .wallet import SolanaRpc

    wallet = str(os.environ["SOLANA_WALLET_ADDRESS"])
    signer = KeyringSolanaSigner(expected_public_key=wallet)
    rpc = SolanaRpc(os.getenv("SOLANA_RPC_HTTP_URL", "https://api.mainnet-beta.solana.com"))
    amount_raw = int(policy.amount_usd * (10**USDC_DECIMALS))
    usdc = await rpc.token_balance(wallet, USDC_MINT)
    if usdc.raw_amount < amount_raw:
        raise ValueError("wallet USDC balance is below $1")
    existing = await rpc.token_balance(wallet, mint)
    if existing.raw_amount > 0:
        raise ValueError("wallet already holds the candidate; cost-basis mixing blocked")
    output_decimals = await rpc.mint_decimals(mint)

    buyer = SolanaAutoBuyer(
        client=JupiterSwapClient(api_key=str(os.environ["JUPITER_API_KEY"])),
        signer=signer,
        max_price_impact_pct=policy.maximum_price_impact_pct,
        max_slippage_bps=policy.maximum_slippage_bps,
    )
    intent = BuyIntent(
        mint=mint,
        symbol=symbol,
        event_key=f"agent-live-canary:{mint}",
        amount_usdc_raw=amount_raw,
        funding_source="one-time-live-canary",
    )
    preflight = await buyer.preflight(intent, rpc)
    result: dict[str, Any] = {
        "mode": "live-canary-preflight",
        "broadcast": False,
        "wallet": signer.public_key,
        "mint": mint,
        "symbol": symbol,
        "input_usdc": policy.amount_usd,
        "liquidity_usd": float(candidate.get("liquidity_usd") or 0),
        "price_impact_pct": preflight.prepared.price_impact_pct,
        "slippage_bps": preflight.prepared.slippage_bps,
        "simulation_units_consumed": preflight.units_consumed,
    }
    if not execute:
        return result

    attempt = {**result, "attempted_at": time.time(), "status": "PENDING"}
    journal.record(attempt)
    store = SQLiteStore(os.getenv("DATABASE_PATH", "launch_guard.db"))
    event_key = intent.event_key
    claimed = store.begin_auto_buy_execution(
        event_key=event_key,
        token_address=mint,
        symbol=symbol,
        funding_source=intent.funding_source,
        input_usdc_raw=preflight.prepared.input_amount_raw,
        expected_output_raw=preflight.prepared.expected_output_raw,
    )
    if not claimed:
        store.close()
        raise ValueError("the live canary execution was already claimed")
    try:
        receipt = await buyer.execute(preflight.prepared)
        if receipt.output_amount_raw <= 0:
            raise RuntimeError("confirmed canary reported no token output")
    except JupiterExecutionError as exc:
        store.freeze_auto_buy_execution(
            event_key=event_key,
            error=str(exc),
            signature=exc.signature,
        )
        store.close()
        journal.record(
            {
                "attempted_at": time.time(),
                "status": "REVIEW_REQUIRED",
                "mint": mint,
                "signature": exc.signature,
                "reason": str(exc),
            }
        )
        raise RuntimeError(
            "live canary outcome is uncertain; do not retry until the journal and "
            "on-chain history are reconciled"
        ) from exc
    except (ConnectionError, RuntimeError, ValueError) as exc:
        store.freeze_auto_buy_execution(
            event_key=event_key,
            error=str(exc),
            signature=getattr(exc, "signature", None),
        )
        store.close()
        raise RuntimeError(
            "live canary outcome requires review; do not retry until reconciled"
        ) from exc
    journal.record(
        {
            "attempted_at": time.time(),
            "status": "CONFIRMED_CHAIN",
            "mint": mint,
            "signature": receipt.signature,
            "input_usdc_raw": receipt.input_amount_raw,
            "output_amount_raw": receipt.output_amount_raw,
        }
    )
    store.complete_auto_buy_execution(
        event_key=event_key,
        signature=receipt.signature,
        actual_output_raw=receipt.output_amount_raw,
        output_decimals=output_decimals,
    )
    quantity = receipt.output_amount_raw / (10**output_decimals)
    cost_usdc = receipt.input_amount_raw / (10**USDC_DECIMALS)
    store.save_owned_holding(
        OwnedHolding(
            chain="solana",
            token_address=mint,
            symbol=symbol,
            quantity=quantity,
            entry_price=cost_usdc / quantity,
            price_currency="USD",
            cost_amount=cost_usdc,
        )
    )
    store.arm_auto_sell(mint, reset_stage=True)
    store.clear_auto_sell_signal_confirmation(mint)
    store.close()
    journal.record(
        {
            "attempted_at": time.time(),
            "status": "CONFIRMED",
            "mint": mint,
            "signature": receipt.signature,
            "input_usdc_raw": receipt.input_amount_raw,
            "output_amount_raw": receipt.output_amount_raw,
        }
    )
    return {
        **result,
        "mode": "live-canary",
        "broadcast": True,
        "signature": receipt.signature,
        "output_amount_raw": receipt.output_amount_raw,
    }
