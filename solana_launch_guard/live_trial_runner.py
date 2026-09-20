"""Fail-closed eight-hour agent trial coordinator.

Broadcast is reachable only through the explicit live_trial start command.
The wallet preflight and human review must happen before that command is used
on a funded mainnet machine.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from .agents import (
    AgentCoordinator, AgentRecord, AgentRole, RiskArbiter, RiskPolicy,
    RiskSnapshot, TradeAction,
)
from .hunter_shadow_strategy import ShadowRecoveryPolicy, assess_entry
from .live_trial_ledger import LiveTrialLedger, TrialHalted
from .execution import BuyIntent, USDC_MINT, PortfolioSignalExitPlanner
from dataclasses import replace


SOLANA_ADDRESS = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}\Z")


@dataclass(frozen=True)
class LiveEntryDecision:
    agent: str
    mint: str
    requested_cents: int
    approved_cents: int
    reason: str
    candidate: dict[str, Any]


def _fresh_candidates(snapshot: dict, *, now: float) -> list[dict]:
    try:
        age = now - float(snapshot.get("generated_at") or 0)
    except (TypeError, ValueError):
        return []
    if not math.isfinite(age) or not 0 <= age <= 15:
        return []
    source = snapshot.get("candidates")
    if not isinstance(source, list):
        return []
    result = []
    for candidate in source:
        if not isinstance(candidate, dict) or candidate.get("chain") != "solana":
            continue
        mint = candidate.get("mint")
        if not isinstance(mint, str) or not SOLANA_ADDRESS.fullmatch(mint):
            continue
        try:
            quote_age = now - float(candidate.get("quoted_at") or 0)
            liquidity = float(candidate.get("liquidity_usd") or 0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(quote_age) and 0 <= quote_age <= 15 and math.isfinite(liquidity) and liquidity >= 50_000:
            result.append(candidate)
    return result


def _live_arbiter() -> RiskArbiter:
    # Live is enabled only for this explicitly constructed trial arbiter; the
    # ordinary shadow arbiter remains paper/shadow-only.
    #
    # max_quote_age_seconds is checked after cycle() has already awaited a
    # full model.propose() round trip to get an exit/entry proposal, so the
    # elapsed time it measures always includes that call's latency, not just
    # market-data staleness. 15s left too little headroom for a normal LLM
    # response and caused a live exit to fail this check - and, unlike the
    # softer freshness gate checked just before it, that halts the whole
    # trial rather than just skipping the one exit. 30s keeps this a real,
    # tight bound while giving the round trip room to complete.
    return RiskArbiter(RiskPolicy(
        allowed_modes=("live",), max_order_usd=5, max_position_pct=100,
        max_open_positions=2, min_liquidity_usd=50_000,
        max_price_impact_pct=3, max_quote_age_seconds=30,
    ))


def decide_hunter_entry(
    snapshot: dict, *, model, ledger: LiveTrialLedger,
    now: float | None = None,
) -> LiveEntryDecision | None:
    """Require a BUY_READY recovery, model proposal, then live arbitration."""
    at = time.time() if now is None else now
    ledger.assert_active(now=at)
    if ledger.unresolved():
        raise TrialHalted("unresolved order requires on-chain reconciliation")
    policy = ShadowRecoveryPolicy.from_env()
    ready = [c for c in _fresh_candidates(snapshot, now=at)
             if assess_entry(c, policy)["state"] == "BUY_READY"]
    if not ready:
        return None
    status = ledger.status(now=at)["agents"]["hunter-v1"]
    if status["remaining_buy_cap_cents"] <= 0 or status["open_positions"] >= 2:
        return None
    candidate = ready[0]
    liquidity = float(candidate["liquidity_usd"])
    review = assess_entry(candidate, policy)
    coordinator = AgentCoordinator(model, _live_arbiter())
    proposal, arbitration = coordinator.ask(
        AgentRecord("hunter-v1", AgentRole.OPPORTUNITY_HUNTER),
        {"mode": "live_trial", "candidate": candidate,
         "recovery_review": review,
         "maximum_order_usd": 5,
         "remaining_gross_budget_usd": status["remaining_buy_cap_cents"] / 100},
        RiskSnapshot(mode="live", equity_usd=30,
                     open_positions=status["open_positions"],
                     daily_realized_pnl_usd=ledger.daily_realized_cents("hunter-v1", now=at) / 100,
                     liquidity_usd=liquidity,
                     quote_age_seconds=at - float(candidate["quoted_at"])),
    )
    ledger.log(agent="hunter-v1", mint=proposal.mint, state="PROPOSAL", reason=proposal.thesis)
    if proposal.action is not TradeAction.BUY:
        return None
    if not arbitration.approved or proposal.mint != candidate["mint"]:
        ledger.log(agent="hunter-v1", mint=proposal.mint, state="BLOCKED",
                   reason="; ".join(arbitration.reasons) if proposal.mint == candidate["mint"]
                   else "proposal mint is not the freshly verified candidate")
        return None
    # Centre of the decision must still pass deterministic entry checks after
    # model latency; an old model output never becomes permission to trade.
    if not _fresh_candidates(snapshot, now=time.time()) or assess_entry(candidate, policy)["state"] != "BUY_READY":
        ledger.log(agent="hunter-v1", mint=proposal.mint, state="BLOCKED", reason="candidate is stale")
        return None
    approved_cents = min(round(arbitration.approved_usd * 100),
                         round(proposal.requested_usd * 100),
                         status["remaining_buy_cap_cents"], 500)
    requested_cents = round(proposal.requested_usd * 100)
    if not 0 < approved_cents <= requested_cents:
        return None
    ledger.log(agent="hunter-v1", mint=proposal.mint, state="APPROVED",
               reason=f"{review['entry_confirmations']} confirmations; approved {approved_cents} cents")
    return LiveEntryDecision("hunter-v1", proposal.mint, requested_cents,
                             approved_cents, proposal.thesis, candidate)


def can_submit(ledger: LiveTrialLedger, *, mint: str, snapshot: dict,
               agent: str = "hunter-v1") -> bool:
    """Call immediately before any broadcast, after signing and simulation."""
    ledger.assert_active()
    if ledger.unresolved() and not (
        len(ledger.unresolved()) == 1
        and ledger.unresolved()[0]["mint"] == mint
        and ledger.unresolved()[0]["agent"] == agent
        and ledger.unresolved()[0]["state"] == "RESERVED"
    ):
        raise TrialHalted("another live order is unresolved")
    policy = ShadowRecoveryPolicy.from_env()
    matching = [c for c in _fresh_candidates(snapshot, now=time.time()) if c["mint"] == mint]
    return bool(matching and assess_entry(matching[0], policy)["state"] == "BUY_READY")


def _token_delta(transaction: dict, *, mint: str, wallet: str) -> int:
    """Read an owner's raw token delta from independently fetched chain data."""
    meta = transaction.get("meta")
    if not isinstance(meta, dict) or meta.get("err") is not None:
        raise TrialHalted("confirmed transaction failed or has no metadata")
    def amounts(field: str) -> dict[int, int]:
        result = {}
        for value in meta.get(field, []):
            if value.get("mint") == mint and value.get("owner") == wallet:
                result[int(value["accountIndex"])] = int(value["uiTokenAmount"]["amount"])
        return result
    before, after = amounts("preTokenBalances"), amounts("postTokenBalances")
    return sum(after.values()) - sum(before.values())


async def execute_hunter_entry(
    decision: LiveEntryDecision, *, ledger: LiveTrialLedger,
    rpc, buyer, store, wallet: str,
    current_snapshot: Callable[[], dict],
) -> dict:
    """One reserved $5-or-less buy with a guarded reverse quote and chain proof.

    Any error after the durable order claim halts the session for manual
    reconciliation, even if the request failed before a transaction was sent.
    """
    from .agent_live_test import CanaryPolicy, validate_exit_quote
    from .portfolio import OwnedHolding

    if decision.agent != "hunter-v1" or not SOLANA_ADDRESS.fullmatch(decision.mint):
        raise ValueError("only a validated hunter Solana mint is eligible")
    ledger.assert_active()
    if buyer.max_price_impact_pct > 3 or buyer.max_slippage_bps > 300:
        raise TrialHalted("live buyer exceeds guarded price impact or slippage limits")
    amount_raw = decision.approved_cents * 10_000  # 1 cent = 10,000 USDC raw units
    usdc = await rpc.token_balance(wallet, USDC_MINT)
    existing = await rpc.token_balance(wallet, decision.mint)
    if usdc.raw_amount < amount_raw or existing.raw_amount > 0:
        raise TrialHalted("insufficient USDC or token already held; cost-basis mixing blocked")
    decimals = await rpc.mint_decimals(decision.mint)
    if not 0 <= decimals <= 18:
        raise TrialHalted("unsupported token decimals")
    intent_key = f"live-trial:hunter:{decision.mint}"
    intent = BuyIntent(mint=decision.mint,
                       symbol=str(decision.candidate.get("symbol") or decision.mint[:8])[:40],
                       event_key=intent_key, amount_usdc_raw=amount_raw,
                       funding_source="live-trial")
    preflight = await buyer.preflight(intent, rpc)
    if preflight.prepared.input_amount_raw != amount_raw:
        raise TrialHalted("buy quote amount differs from approved capital")
    reverse = await buyer.client.order(input_mint=decision.mint,
                                       output_mint=USDC_MINT,
                                       amount_raw=preflight.prepared.minimum_output_raw)
    validate_exit_quote(reverse, mint=decision.mint,
                        amount_raw=preflight.prepared.minimum_output_raw,
                        usdc_mint=USDC_MINT,
                        policy=CanaryPolicy())
    if not can_submit(ledger, mint=decision.mint, snapshot=current_snapshot()):
        raise TrialHalted("BUY_READY recovery expired before reservation")
    ledger.reserve_buy(intent=intent_key, agent="hunter-v1", mint=decision.mint,
                       requested_cents=decision.requested_cents,
                       approved_cents=decision.approved_cents)
    ledger.log(agent="hunter-v1", mint=decision.mint, state="RESERVED",
               reason=f"reserved {decision.approved_cents} cents before possible broadcast")
    try:
        if not store.begin_auto_buy_execution(
            event_key=intent_key, token_address=decision.mint, symbol=intent.symbol,
            funding_source=intent.funding_source, input_usdc_raw=amount_raw,
            expected_output_raw=preflight.prepared.expected_output_raw,
        ):
            raise TrialHalted("buy intent already claimed in main execution database")
        refreshed = await rpc.token_balance(wallet, USDC_MINT)
        refreshed_token = await rpc.token_balance(wallet, decision.mint)
        if (refreshed.raw_amount < amount_raw or refreshed_token.raw_amount > 0
            or not can_submit(ledger, mint=decision.mint, snapshot=current_snapshot())):
            raise TrialHalted("wallet or signal changed before buy broadcast")
        receipt = await buyer.execute(preflight.prepared)
        ledger.transition(intent_key, "SUBMITTED", signature=receipt.signature)
        ledger.log(agent="hunter-v1", mint=decision.mint, state="SUBMITTED",
                   reason=f"public transaction signature {receipt.signature}")
        chain = await rpc.get_transaction(receipt.signature)
        if not isinstance(chain, dict):
            raise TrialHalted("submitted buy is not yet visible from RPC; reconcile on chain")
        usdc_delta = _token_delta(chain, mint=USDC_MINT, wallet=wallet)
        token_delta = _token_delta(chain, mint=decision.mint, wallet=wallet)
        if (not 0 < -usdc_delta <= amount_raw or token_delta <= 0
            or receipt.input_amount_raw != -usdc_delta
            or receipt.output_amount_raw != token_delta):
            raise TrialHalted("chain balance deltas differ from confirmed buy receipt")
        spent_cents = math.ceil(-usdc_delta / 10_000)
        entry_price = (-usdc_delta / 1_000_000) / (token_delta / 10**decimals)
        store.complete_auto_buy_execution(
            event_key=intent_key, signature=receipt.signature,
            actual_output_raw=token_delta, output_decimals=decimals,
        )
        store.save_owned_holding(OwnedHolding(
            chain="solana", token_address=decision.mint, symbol=intent.symbol,
            quantity=token_delta / 10**decimals, entry_price=entry_price,
            price_currency="USD", cost_amount=-usdc_delta / 1_000_000,
        ))
        store.arm_auto_sell(decision.mint, reset_stage=True)
        ledger.confirm_buy(intent=intent_key, signature=receipt.signature,
                           executed_cents=spent_cents, quantity_raw=token_delta,
                           decimals=decimals, entry_price=entry_price,
                           entry_liquidity_usd=float(decision.candidate["liquidity_usd"]),
                           verified_on_chain=True)
        ledger.log(agent="hunter-v1", mint=decision.mint, state="CONFIRMED",
                   reason=f"chain verified buy: {spent_cents} cents, {token_delta} raw tokens")
        return {"status": "CONFIRMED", "mint": decision.mint,
                "signature": receipt.signature, "spent_cents": spent_cents,
                "quantity_raw": token_delta}
    except BaseException as exc:
        # No retry until an operator reconciles the wallet, main database and
        # trial ledger. A missing signature is not proof that no trade landed.
        if ledger.unresolved():
            ledger.halt("buy outcome requires wallet and chain reconciliation")
        ledger.log(agent="hunter-v1", mint=decision.mint, state="HALTED",
                   reason=str(exc)[:500])
        raise


async def execute_live_exit(
    *, ledger: LiveTrialLedger, agent: str, mint: str, symbol: str,
    decision: str, position_value_usd: float, quote_age_seconds: float,
    liquidity_usd: float, fraction: float, rpc, seller, store, wallet: str,
    current_exit_allowed: Callable[[], bool],
    minimum_sell_usd: float = 2.0,
    max_price_impact_pct: float = 5.0,
    max_slippage_bps: int = 500,
    stage_key: str = "",
) -> dict:
    """One owned-token exit; new-buy cap is inapplicable to existing holdings."""
    if not SOLANA_ADDRESS.fullmatch(mint) or agent not in {"hunter-v1", "copy-v1", "portfolio-v1"}:
        raise ValueError("invalid Solana owned-position exit")
    if decision not in {"SELL", "TAKE_PARTIAL", "EMERGENCY_EXIT"}:
        raise ValueError("no eligible live exit decision")
    if stage_key not in {"", "PRINCIPAL", "SECOND_STAGE"} or (stage_key and decision != "TAKE_PARTIAL"):
        raise ValueError("invalid partial profit stage")
    if not math.isfinite(fraction) or not 0 < fraction <= 1:
        raise ValueError("invalid owned position exit fraction")
    if not current_exit_allowed():
        raise TrialHalted("EXIT BLOCKED: exit signal expired or changed before reservation")
    ledger.assert_active()
    if (seller.max_price_impact_pct > max_price_impact_pct
        or seller.max_slippage_bps > max_slippage_bps):
        raise TrialHalted("live seller exceeds guarded price impact or slippage limits")
    approval = _live_arbiter().evaluate_live_exit(
        position_value_usd * fraction,
        RiskSnapshot(mode="live", current_position_usd=position_value_usd,
                     quote_age_seconds=quote_age_seconds,
                     liquidity_usd=liquidity_usd),
    )
    if not approval.approved:
        ledger.log(agent=agent, mint=mint, state="EXIT_BLOCKED", reason="; ".join(approval.reasons))
        # Market-condition-dependent (quote age, price impact, position
        # value) - these can pass on a later cycle once fresh data is in, so
        # this is deliberately an "EXIT BLOCKED"-prefixed halt: _guarded_exit
        # only halts the whole trial for messages that *don't* start with
        # that prefix, so this one skips just this mint and retries next
        # cycle instead of stopping everything else the trial could do.
        raise TrialHalted(
            f"EXIT BLOCKED: failed deterministic risk checks "
            f"({'; '.join(approval.reasons)})"
        )
    ledger.log(agent=agent, mint=mint, state="APPROVED",
               reason=f"{decision}: guarded owned-position exit of {approval.approved_usd:.2f} USD")
    balance = await rpc.token_balance(wallet, mint)
    if balance.raw_amount <= 0:
        raise TrialHalted("configured wallet has no tokens to exit")
    tracked = next((p for p in ledger.positions(agent) if p["mint"] == mint), None)
    if tracked and (balance.raw_amount != tracked["quantity_raw"] or balance.decimals != tracked["decimals"]):
        raise TrialHalted("agent position and on-chain balance differ; manual reconciliation required")
    plan = PortfolioSignalExitPlanner(take_partial_fraction=fraction,
                                      exit_warning_fraction=fraction).plan(
        mint=mint, symbol=symbol[:40],
        decision="TAKE PARTIAL" if decision == "TAKE_PARTIAL" else "EXIT WARNING",
        reason="bounded live trial owned-position exit", balance_raw=balance.raw_amount,
        decimals=balance.decimals,
    )
    if plan is None:
        raise TrialHalted("no positive token amount can be sold")
    # The signal identity must be stable across the loop and after restart.
    # Repeated model calls for the same mint and stage cannot execute twice.
    key = f"live-trial:{agent}:sell:{mint}:{decision}" + (f":{stage_key}" if stage_key else "")
    existing = ledger.db.execute("SELECT state FROM orders WHERE intent=?", (key,)).fetchone()
    if existing:
        if existing[0] == "CONFIRMED":
            raise TrialHalted("EXIT BLOCKED: this exit stage was already confirmed")
        raise TrialHalted("a previous exit stage is unresolved; reconcile on chain")
    plan = replace(plan, event_key=key)
    preflight = await seller.preflight(plan, rpc)
    prepared = preflight.prepared
    floor_raw = math.ceil(max(2.0, minimum_sell_usd) * 1_000_000)
    if prepared.minimum_output_raw < floor_raw:
        raise TrialHalted("EXIT BLOCKED: minimum quoted proceeds are below the configured sell floor")
    if (prepared.quoted_price_impact_pct is None
        or abs(prepared.quoted_price_impact_pct) > max_price_impact_pct
        or prepared.quoted_slippage_bps is None
        or prepared.quoted_slippage_bps > max_slippage_bps):
        raise TrialHalted("EXIT BLOCKED: exact quote impact or slippage exceeds live limits")
    if not current_exit_allowed():
        raise TrialHalted("EXIT BLOCKED: exit signal changed during sell simulation")
    ledger.reserve_sell(intent=key, agent=agent, mint=mint)
    ledger.log(agent=agent, mint=mint, state="RESERVED",
               reason=f"exit {decision}; token amount {prepared.input_amount_raw}")
    try:
        if not store.begin_auto_sell_execution(
            event_key=key, chain="solana", token_address=mint,
            symbol=plan.symbol, stage=plan.stage, requested_raw=prepared.input_amount_raw,
            expected_output_raw=prepared.expected_output_raw,
            balance_before_raw=balance.raw_amount,
        ):
            raise TrialHalted("sell intent was already claimed in main execution database")
        updated = await rpc.token_balance(wallet, mint)
        if updated.raw_amount != balance.raw_amount or not current_exit_allowed():
            raise TrialHalted("wallet balance or exit signal changed before broadcast")
        ledger.assert_active()
        sale = await seller.execute(prepared)
        ledger.transition(key, "SUBMITTED", signature=sale.signature)
        ledger.log(agent=agent, mint=mint, state="SUBMITTED",
                   reason=f"public transaction signature {sale.signature}")
        transaction = await rpc.get_transaction(sale.signature)
        if not isinstance(transaction, dict):
            raise TrialHalted("submitted sell is not yet visible from RPC; reconcile on chain")
        token_delta = _token_delta(transaction, mint=mint, wallet=wallet)
        usdc_delta = _token_delta(transaction, mint=USDC_MINT, wallet=wallet)
        if (token_delta >= 0 or -token_delta != sale.input_amount_raw
            or usdc_delta <= 0 or usdc_delta != sale.output_amount_raw
            or usdc_delta < floor_raw):
            raise TrialHalted("on-chain token and USDC changes differ from sell receipt")
        store.complete_auto_sell_execution(event_key=key, signature=sale.signature,
                                            next_stage=plan.stage + 1)
        ledger.confirm_sell(intent=key, signature=sale.signature,
                            quantity_raw=-token_delta,
                            proceeds_cents=usdc_delta // 10_000,
                            verified_on_chain=True)
        ledger.log(agent=agent, mint=mint, state="CONFIRMED",
                   reason=f"chain verified {decision}, proceeds {usdc_delta} USDC raw")
        return {"status": "CONFIRMED", "decision": decision,
                "mint": mint, "signature": sale.signature,
                "proceeds_usdc_raw": usdc_delta, "tokens_sold_raw": -token_delta}
    except BaseException as exc:
        if ledger.unresolved():
            ledger.halt("sell outcome requires wallet and chain reconciliation")
        ledger.log(agent=agent, mint=mint, state="HALTED", reason=str(exc)[:500])
        raise
