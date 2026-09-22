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

# Even an emergency liquidation (wider slippage/impact ceiling, no model
# gate) must refuse a quote implying an implausible loss versus the
# position's own marked value - that's the signature of a broken quote or
# a stale/wrong oracle price, not a real market. This catches that case
# regardless of which ceiling let the quote through in the first place.
EMERGENCY_MIN_PROCEEDS_FRACTION = 0.25


@dataclass(frozen=True)
class LiveEntryDecision:
    agent: str
    mint: str
    requested_cents: int
    approved_cents: int
    reason: str
    candidate: dict[str, Any]


def _fresh_candidates(
    snapshot: dict, *, now: float, min_liquidity_usd: float = 50_000.0
) -> list[dict]:
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
        if math.isfinite(quote_age) and 0 <= quote_age <= 15 and math.isfinite(liquidity) and liquidity >= min_liquidity_usd:
            result.append(candidate)
    return result


def _live_arbiter(max_quote_age_seconds: int = 15, min_liquidity_usd: float = 50_000.0) -> RiskArbiter:
    # Live is enabled only for this explicitly constructed trial arbiter; the
    # ordinary shadow arbiter remains paper/shadow-only.
    #
    # The 15s default is genuine market-data staleness: decide_hunter_entry
    # builds its RiskSnapshot before calling model.propose(), so this bound
    # never has to absorb that call's latency. The exit path is different -
    # execute_live_exit's callers compute quote_age_seconds AFTER a
    # model.propose() round trip, so that elapsed time always includes the
    # call's latency, not just market-data staleness; 15s there left too
    # little headroom for a normal LLM response and caused a live exit to
    # fail this check, which (unlike the softer freshness gate checked just
    # before it) halts the whole trial rather than just skipping the one
    # exit. Callers on the exit path pass max_quote_age_seconds=30 to give
    # the round trip room to complete without loosening the entry path's
    # genuine staleness bound.
    return RiskArbiter(RiskPolicy(
        allowed_modes=("live",), max_order_usd=5, max_position_pct=100,
        max_open_positions=2, min_liquidity_usd=min_liquidity_usd,
        max_price_impact_pct=9, max_quote_age_seconds=max_quote_age_seconds,
        # Default (3% of $30 equity = $0.90) was smaller than a single
        # normal stop-loss on a $5 position - tonight's real losses were
        # $1.44-1.45 (28-34% drawdowns), so one trade always exhausted the
        # whole day's allowance and blocked every other candidate for the
        # rest of the day, including ones never even attempted. 10% ($3.00)
        # absorbs about two bad trades before halting for the day instead
        # of one.
        max_daily_loss_pct=10,
    ))


def decide_hunter_entry(
    snapshot: dict, *, model, ledger: LiveTrialLedger,
    now: float | None = None,
    buy_zone_skip_reasons: dict[str, str] | None = None,
    chase_first_target: dict[str, float] | None = None,
    current_snapshot: Callable[[], dict] | None = None,
) -> LiveEntryDecision | None:
    """Require a BUY_READY recovery, model proposal, then live arbitration."""
    at = time.time() if now is None else now
    ledger.assert_active(now=at)
    if ledger.unresolved():
        raise TrialHalted("unresolved order requires on-chain reconciliation")
    policy = ShadowRecoveryPolicy.from_env()
    assessed = [
        (c, assess_entry(c, policy))
        for c in _fresh_candidates(snapshot, now=at, min_liquidity_usd=policy.min_liquidity_usd)
    ]
    ready = [c for c, review in assessed if review["state"] == "BUY_READY"]
    if not ready:
        # A candidate can already show BUY ZONE/BUY NOW in the recommendation
        # feed (and so have already sent a phone alert) while still failing
        # this stricter, independent live-entry policy. Without this, that
        # gap is invisible - it silently returns None with no ledger entry
        # at all, leaving "why didn't the trial buy that?" unanswerable from
        # --live-trial-status alone.
        #
        # But logging this on every cycle a candidate stays stuck (routine -
        # e.g. waiting on liquidity/volume for several cycles) would grow the
        # ledger unbounded and crowd PROPOSAL/CONFIRMED-BUY rows out of the
        # last-10 recent_decisions view. Only log when the reason actually
        # changes for that mint, mirroring _track_exit_block_streak's
        # "don't repeat, but don't go silent either" approach.
        skip_reasons = buy_zone_skip_reasons if buy_zone_skip_reasons is not None else {}
        for candidate, review in assessed:
            if candidate.get("decision") in {"BUY ZONE", "BUY NOW", "MOMENTUM BUY"} and review["state"] != "BUY_READY":
                mint = str(candidate.get("mint") or "")
                reason = "; ".join(review["reasons"])
                if skip_reasons.get(mint) == reason:
                    continue
                skip_reasons[mint] = reason
                ledger.log(agent="hunter-v1", mint=mint,
                           state="BUY_ZONE_SKIPPED", reason=reason)
        return None
    status = ledger.status(now=at)["agents"]["hunter-v1"]
    if status["remaining_buy_cap_cents"] <= 0 or status["open_positions"] >= 2:
        return None
    candidate = ready[0]
    mint = str(candidate.get("mint") or "")
    # Freeze the target price the first time this mint becomes buyable, and
    # compare every later retry against that frozen value, not the live one -
    # the recommendation engine re-anchors planned_target_price to a new peak
    # whenever price makes a fresh high, so comparing against the live value
    # would let the target chase the price up forever and never trigger. A
    # token still climbing toward the entry we originally planned for is
    # exactly what we want to keep retrying on; one that's already blown
    # past what we ourselves judged worth exiting at is not - we'd be buying
    # at what would have been our own take-profit level.
    chase_targets = chase_first_target if chase_first_target is not None else {}
    frozen_target = chase_targets.setdefault(mint, candidate.get("planned_target_price"))
    current_price = candidate.get("price")
    if (frozen_target is not None and current_price is not None
            and float(current_price) >= float(frozen_target)):
        ledger.log(agent="hunter-v1", mint=mint, state="BLOCKED",
                   reason=f"price {float(current_price):.12g} reached the original planned "
                          f"target {float(frozen_target):.12g} before a fill; abandoning the "
                          "chase rather than buying at what would have been our own exit level")
        return None
    liquidity = float(candidate["liquidity_usd"])
    review = assess_entry(candidate, policy)
    coordinator = AgentCoordinator(model, _live_arbiter(min_liquidity_usd=policy.min_liquidity_usd))
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
    # This re-reads the recommendation feed from disk (when a current_snapshot
    # callable is given) instead of re-checking the age of the same snapshot
    # this function started with: the model call reliably takes ~10-15s, and
    # re-timestamping the *original* snapshot against a later "now" fails
    # that snapshot's freshness bound purely from that wait, even though a
    # newer, genuinely fresh read exists on disk showing the candidate is
    # still perfectly valid. Re-fetching checks whether the trade is still
    # good *right now*, not whether the data we started with has aged out.
    recheck_snapshot = current_snapshot() if current_snapshot is not None else snapshot
    fresh = _fresh_candidates(recheck_snapshot, now=time.time(), min_liquidity_usd=policy.min_liquidity_usd)
    fresh_candidate = next((c for c in fresh if c.get("mint") == candidate.get("mint")), None)
    if fresh_candidate is None or assess_entry(fresh_candidate, policy)["state"] != "BUY_READY":
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
    matching = [
        c for c in _fresh_candidates(snapshot, now=time.time(), min_liquidity_usd=policy.min_liquidity_usd)
        if c["mint"] == mint
    ]
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
    if buyer.max_price_impact_pct > 9 or buyer.max_slippage_bps > 1700:
        raise TrialHalted("live buyer exceeds guarded price impact or slippage limits")
    amount_raw = decision.approved_cents * 10_000  # 1 cent = 10,000 USDC raw units
    usdc = await rpc.token_balance(wallet, USDC_MINT)
    existing = await rpc.token_balance(wallet, decision.mint)
    if usdc.raw_amount < amount_raw or existing.raw_amount > 0:
        raise TrialHalted("insufficient USDC or token already held; cost-basis mixing blocked")
    decimals = await rpc.mint_decimals(decision.mint)
    if not 0 <= decimals <= 18:
        raise TrialHalted("unsupported token decimals")
    # Scoped to this ledger's own session, not just the mint: the main
    # execution database's claim on this key is a one-shot, permanent lock
    # (INSERT OR IGNORE on a PRIMARY KEY), so a key that doesn't vary per
    # session would mean a stopped-and-restarted trial inherits a stale claim
    # from a past attempt at the same mint and can never retry it again -
    # path.stem alone doesn't do this, since the live ledger is always
    # recreated at the same filename once the old one is archived away;
    # started_at is the part that actually changes across a restart. This
    # relies on the fresh on-chain wallet-balance check above (not this key)
    # to stop an actual double-buy of a mint a past session really completed.
    intent_key = f"live-trial:{ledger.path.stem}:{ledger.started_at()}:hunter:{decision.mint}"
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
        # A trial session that started before the ledger-scoped key format was
        # introduced may already hold this mint's claim under the old,
        # unscoped key. That claim predates this process and is invisible to
        # the scoped lookup below, so check it explicitly - otherwise a
        # restart against the same still-open trial could re-buy a mint it
        # already bought.
        legacy_intent_key = f"live-trial:hunter:{decision.mint}"
        legacy_claimed = store.connection.execute(
            "SELECT 1 FROM auto_buy_executions WHERE event_key = ?", (legacy_intent_key,),
        ).fetchone()
        if legacy_claimed or not store.begin_auto_buy_execution(
            event_key=intent_key, token_address=decision.mint, symbol=intent.symbol,
            funding_source=intent.funding_source, input_usdc_raw=amount_raw,
            expected_output_raw=preflight.prepared.expected_output_raw,
        ):
            raise TrialHalted("buy intent already claimed in main execution database")
        refreshed = await rpc.token_balance(wallet, USDC_MINT)
        refreshed_token = await rpc.token_balance(wallet, decision.mint)
        if refreshed.raw_amount < amount_raw:
            # Real wallet USDC came up short against a reservation the ledger
            # already believes is good - that's a mismatch between our own
            # accounting and on-chain reality, not routine market movement,
            # so it stays a full-trial halt for manual reconciliation.
            raise TrialHalted("USDC balance dropped below the reserved buy before broadcast")
        if refreshed_token.raw_amount > 0 or not can_submit(
            ledger, mint=decision.mint, snapshot=current_snapshot()
        ):
            # Either condition just means this one candidate is no longer
            # buyable right now (someone/something else already holds it, or
            # the market moved past the entry in the last second) - the same
            # outcome the pre-reservation checks above treat as routine. Release
            # the reservation so the trial keeps running instead of halting.
            reason = ("token already held" if refreshed_token.raw_amount > 0
                     else "BUY_READY signal expired")
            ledger.transition(intent_key, "FAILED")
            raise ValueError(f"{reason} before broadcast; reservation released")
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
    current_decision: Callable[[], str | None] | None = None,
    minimum_sell_usd: float = 2.0,
    max_price_impact_pct: float = 8.0,
    max_slippage_bps: int = 1500,
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
    # A full liquidation (SELL/EMERGENCY_EXIT of the entire remaining
    # position) isn't held to the usual $2 minimum-order-size floor - a
    # position that crashed below that floor still needs to be sellable,
    # or it gets stuck forever. It keeps a much smaller dust guard so a
    # swap quoting essentially nothing (below gas cost) still doesn't
    # fire. A discretionary partial sell keeps the full $2 floor.
    is_full_liquidation = decision in {"SELL", "EMERGENCY_EXIT"} and fraction >= 0.999

    def _exit_signal_stale() -> bool:
        # The eligibility gate alone tolerates a relabel between any
        # sell-worthy decision (a routine relabel, not a change of mind) but
        # `fraction` was sized for the ORIGINAL decision, computed once
        # before this reservation began. If the signal has since escalated
        # from a partial (TAKE_PARTIAL) to a full exit (EXIT WARNING), that
        # stale, too-small fraction must not be allowed to execute - block
        # so the next cycle proposes a fresh, correctly-sized full exit.
        if current_decision is None or decision != "TAKE_PARTIAL":
            return False
        return current_decision() == "EXIT WARNING"

    if not current_exit_allowed() or _exit_signal_stale():
        raise TrialHalted("EXIT BLOCKED: exit signal expired or changed before reservation")
    ledger.assert_active()
    if (seller.max_price_impact_pct > max_price_impact_pct
        or seller.max_slippage_bps > max_slippage_bps):
        raise TrialHalted("live seller exceeds guarded price impact or slippage limits")
    approval = _live_arbiter(max_quote_age_seconds=30).evaluate_live_exit(
        position_value_usd * fraction,
        RiskSnapshot(mode="live", current_position_usd=position_value_usd,
                     quote_age_seconds=quote_age_seconds,
                     liquidity_usd=liquidity_usd),
        min_amount_usd=0.01 if is_full_liquidation else 2,
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
    tracked = next((p for p in ledger.positions(agent) if p["mint"] == mint), None)
    if tracked and balance.raw_amount < tracked["quantity_raw"]:
        # A wallet balance below what the ledger tracks - including all the
        # way to zero - is what an operator selling manually outside the
        # trial looks like (observed live: the wallet held nothing left to
        # exit right after the operator confirmed a manual sell). There's no
        # proceeds data for a sale this ledger didn't execute, so nothing to
        # record beyond bringing the tracked position back in line with
        # on-chain reality - routine, not an emergency, so it doesn't halt.
        # An *increase* above the tracked amount has no such explanation and
        # still halts below.
        ledger.reconcile_external_reduction(
            agent=agent, mint=mint, wallet_quantity_raw=balance.raw_amount,
            wallet_decimals=balance.decimals,
        )
        ledger.log(agent=agent, mint=mint, state="POSITION_RECONCILED",
                   reason=f"wallet balance {balance.raw_amount} is below the tracked "
                          f"{tracked['quantity_raw']}; treating as a sell outside the trial")
        raise ValueError("position reduced or closed outside the trial; nothing left to exit here")
    if balance.raw_amount <= 0:
        raise TrialHalted("configured wallet has no tokens to exit")
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
    # The signal identity must be stable across the loop and after a restart
    # of *this* ledger, so repeated model calls for the same mint and stage
    # cannot execute twice - but it also must not collide with a different
    # trial session: the main execution database's claim on this key is a
    # one-shot, permanent lock (INSERT OR IGNORE on a PRIMARY KEY), so a key
    # that doesn't vary per session would mean a stopped-and-restarted trial
    # inherits a stale claim from a past attempt at the same mint/stage and
    # can never retry it again. started_at gives both properties at once: it
    # doesn't change if this exact ledger file is simply reopened (same trial
    # row), but a fresh ledger created after the old one is archived away -
    # our actual restart procedure - gets a new one, unlike path.stem, which
    # stays identical either way since the active ledger always lives at the
    # same filename.
    key = (f"live-trial:{ledger.path.stem}:{ledger.started_at()}:{agent}:sell:{mint}:{decision}"
           + (f":{stage_key}" if stage_key else ""))
    existing = ledger.db.execute("SELECT state FROM orders WHERE intent=?", (key,)).fetchone()
    if existing:
        if existing[0] == "CONFIRMED":
            raise TrialHalted("EXIT BLOCKED: this exit stage was already confirmed")
        raise TrialHalted("a previous exit stage is unresolved; reconcile on chain")
    plan = replace(plan, event_key=key)
    preflight = await seller.preflight(plan, rpc)
    prepared = preflight.prepared
    floor_raw = math.ceil((0.01 if is_full_liquidation else max(2.0, minimum_sell_usd)) * 1_000_000)
    if prepared.minimum_output_raw < floor_raw:
        raise TrialHalted("EXIT BLOCKED: minimum quoted proceeds are below the configured sell floor")
    catastrophic_floor_raw = math.ceil(
        position_value_usd * fraction * EMERGENCY_MIN_PROCEEDS_FRACTION * 1_000_000
    )
    if prepared.minimum_output_raw < catastrophic_floor_raw:
        raise TrialHalted(
            "EXIT BLOCKED: quoted proceeds imply an implausible loss versus "
            "the marked position value; refusing as a likely pricing anomaly"
        )
    if (prepared.quoted_price_impact_pct is None
        or abs(prepared.quoted_price_impact_pct) > max_price_impact_pct
        or prepared.quoted_slippage_bps is None
        or prepared.quoted_slippage_bps > max_slippage_bps):
        raise TrialHalted("EXIT BLOCKED: exact quote impact or slippage exceeds live limits")
    if not current_exit_allowed() or _exit_signal_stale():
        raise TrialHalted("EXIT BLOCKED: exit signal changed during sell simulation")
    ledger.reserve_sell(intent=key, agent=agent, mint=mint)
    ledger.log(agent=agent, mint=mint, state="RESERVED",
               reason=f"exit {decision}; token amount {prepared.input_amount_raw}")
    try:
        # Same migration hazard as the buy path: a claim recorded before the
        # ledger-scoped key format was introduced is invisible to the scoped
        # lookup below, so check the legacy key explicitly before claiming -
        # otherwise a restart against the same still-open trial could re-sell
        # a stage that was already sold.
        legacy_key = (f"live-trial:{agent}:sell:{mint}:{decision}"
                     + (f":{stage_key}" if stage_key else ""))
        legacy_claimed = store.connection.execute(
            "SELECT 1 FROM auto_sell_executions WHERE event_key = ?", (legacy_key,),
        ).fetchone()
        if legacy_claimed or not store.begin_auto_sell_execution(
            event_key=key, chain="solana", token_address=mint,
            symbol=plan.symbol, stage=plan.stage, requested_raw=prepared.input_amount_raw,
            expected_output_raw=prepared.expected_output_raw,
            balance_before_raw=balance.raw_amount,
        ):
            raise TrialHalted("sell intent was already claimed in main execution database")
        updated = await rpc.token_balance(wallet, mint)
        if updated.raw_amount < balance.raw_amount:
            # Same reasoning as the pre-reservation check above: a wallet
            # balance drop here is what an operator selling manually outside
            # the trial looks like (this reservation's own sell never
            # broadcast, so nothing this trial did caused the discrepancy).
            # Routine, not an emergency - reconcile and release instead of
            # halting the whole trial. An *increase* has no such explanation
            # and still halts below.
            if tracked:
                ledger.reconcile_external_reduction(
                    agent=agent, mint=mint, wallet_quantity_raw=updated.raw_amount,
                    wallet_decimals=updated.decimals,
                )
            ledger.log(agent=agent, mint=mint, state="POSITION_RECONCILED",
                       reason=f"wallet balance dropped from {balance.raw_amount} to "
                              f"{updated.raw_amount} before broadcast; treating as a sell outside the trial")
            ledger.transition(key, "FAILED")
            raise ValueError("wallet balance dropped before broadcast; likely sold outside the trial")
        if updated.raw_amount > balance.raw_amount:
            raise TrialHalted("wallet balance increased before broadcast; manual reconciliation required")
        if not current_exit_allowed() or _exit_signal_stale():
            ledger.transition(key, "FAILED")
            raise ValueError("exit signal expired or changed before broadcast; reservation released")
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
