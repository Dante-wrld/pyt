"""Explicit, single-session Solana agent trial. No automatic activation."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

from openai import APIError

from .agents import AgentRole, TradeAction
from .config import Settings, _load_dotenv
from .core import SQLiteStore
from .execution import (
    AdditionalSignerError, BuyIntent, JupiterSwapClient, KeyringSolanaSigner, SolanaAutoBuyer,
    SolanaAutoSeller, USDC_MINT, WRAPPED_SOL_MINT,
)
from .hunter_shadow_strategy import ShadowRecoveryPolicy, assess_exit
from .live_trial_ledger import LiveTrialLedger, TrialHalted
from .live_trial_runner import (
    LiveEntryDecision, _regrowth_bar_clears, decide_hunter_entry, decide_regrowth_rebuy,
    execute_hunter_entry, execute_live_exit,
)
from .market import DexScreenerOracle
from .wallet import SolanaRpc, TransactionSimulationFailed

MAX_MODEL_REQUESTS = 200

# Decisions that still warrant a sell attempt. The portfolio monitor
# recomputes this label independently every ~15s, and for a small, volatile
# position it can flip between these three (all still "sell it") between
# when a proposal is generated and when the exit is about to execute -
# that's a relabeling, not a change of mind, and must not block the exit.
SELL_WORTHY_DECISIONS = ("EXIT WARNING", "TAKE PARTIAL", "PROTECT PROFIT")

# The wallet-scan below used to treat every sell-worthy holding as fair
# game, regardless of who bought it - it isn't only this trial's own
# positions, since the same wallet is also used for a separate copy-
# trading bot (CopyFomo, on Telegram) and for the operator's own manual
# buys, and this loop has no business managing either. A holding this
# ledger's own buy flow never recorded (see
# SQLiteStore.is_launch_guard_owned) is classified once, the first time
# it's ever seen, and remembered forever rather than re-checked each
# cycle - current value drifts away from whatever was actually paid
# within minutes on these tokens, so only a first-sight check is
# meaningful. CopyFomo's own position sizes are small and predictable
# ($5/$8/$10 with up to ~10% slippage/chase); the operator can keep their
# own manual buys outside these ranges to stay distinguishable.
COPYFOMO_PURCHASE_RANGES_USD = ((5.00, 5.50), (8.00, 8.80), (10.00, 11.00))

# Deterministic emergency-liquidation escalation. Normal exits optimize
# execution quality (tight slippage/impact ceiling, model-approved sizing);
# an emergency exit optimizes the probability of getting out at all, so it
# skips the model round trip entirely and sells the full remaining position
# at a much wider (but still bounded - see execute_live_exit's catastrophic
# floor) ceiling. A position still in profit does not escalate on the
# weaker triggers (a blocked normal attempt, or repeated failures) - only a
# real stop-loss breach or a liquidity collapse can force it, since a
# winning position waiting briefly for a better fill has little to lose.
EMERGENCY_STOP_LOSS_PCT = 20
EMERGENCY_BLOCK_STREAK = 2

# EXIT WARNING positions scanned from the wallet (not bought through this
# ledger) often have no recorded cost basis, so pnl_pct is None and
# _should_escalate_to_emergency can never fire for them no matter how long
# they stay stuck - observed live: two sub-$0.25 dust positions consumed 43
# of a single trial's 100-request model budget, proposing a full exit every
# single cycle for hours with the model recommending the identical "sell
# it" answer every time and execution failing for the same structural
# reason (below the exchange minimum / too illiquid for any slippage
# ceiling) each time. Once a position has given the same obvious signal
# this many consecutive cycles in a row, asking the model again adds no
# information - fall back to the answer it already gave and keep retrying
# the exit deterministically. Only applies to EXIT WARNING (a full,
# unambiguous liquidation): TAKE_PARTIAL/PROTECT PROFIT sizing genuinely
# depends on model judgment and is left untouched.
EXIT_WARNING_MODEL_SKIP_STREAK = 5


def _exit_warning_model_call_is_redundant(signal: str, block_streak: int) -> bool:
    """True once an EXIT WARNING position has repeated the same obvious
    full-exit signal for EXIT_WARNING_MODEL_SKIP_STREAK consecutive blocked
    cycles - asking the model again adds no information (see
    EXIT_WARNING_MODEL_SKIP_STREAK above)."""
    return signal == "EXIT WARNING" and block_streak >= EXIT_WARNING_MODEL_SKIP_STREAK


def _should_escalate_to_emergency(*, pnl_pct: float | None, block_streak: int,
                                  liquidity_collapse: bool = False) -> bool:
    if liquidity_collapse:
        return True
    if pnl_pct is None:
        return False
    if pnl_pct <= -EMERGENCY_STOP_LOSS_PCT and block_streak >= 1:
        return True
    return pnl_pct <= 0 and block_streak >= EMERGENCY_BLOCK_STREAK


class BoundedTrialModel:
    """Persist a finite model-request cap before spending any API credits."""

    def __init__(self, model, ledger: LiveTrialLedger):
        self.model = model
        self.ledger = ledger

    def propose(self, *, role: AgentRole, context: dict) -> dict:
        self.ledger.assert_active()
        if self.ledger.model_request_count() >= MAX_MODEL_REQUESTS:
            raise TrialHalted(f"{MAX_MODEL_REQUESTS} model requests used; trial stopped to protect credits")
        agent = {AgentRole.OPPORTUNITY_HUNTER: "hunter-v1",
                 AgentRole.PORTFOLIO_MANAGER: "portfolio-v1",
                 AgentRole.COPY_TRADER: "copy-v1"}[role]
        self.ledger.log(agent=agent, mint="", state="MODEL_REQUEST",
                        reason="one structured proposal requested")
        return self.model.propose(role=role, context=context)


def _enabled(name: str) -> bool:
    return os.getenv(name, "false").lower() in {"true", "1", "yes", "on"}


def _read_json(path: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return value if isinstance(value, dict) else {}


def _snapshot_is_fresh(snapshot: dict) -> bool:
    try:
        age = time.time() - float(snapshot.get("generated_at") or 0)
    except (ValueError, TypeError):
        return False
    # Same 30s bound as _live_arbiter()'s exit-path max_quote_age_seconds
    # (live_trial_runner.py passes 30 explicitly there; its default is a
    # tighter 15 for the entry path), and for the same reason: _eligible_exit
    # re-checks this snapshot after a model.propose() round trip, so a bound
    # this tight can fail purely from that latency rather than genuine
    # staleness.
    return math.isfinite(age) and 0 <= age <= 30


def require_exclusive_trial_flags() -> None:
    if not _enabled("AGENT_LIVE_TEST_ENABLED") or not _enabled("AGENT_LIVE_TRIAL_ENABLED"):
        raise TrialHalted("AGENT_LIVE_TEST_ENABLED and AGENT_LIVE_TRIAL_ENABLED must both be true")
    if _enabled("AGENT_LIVE_KILL_SWITCH") or os.getenv("AGENT_LIVE_KILL_SWITCH", "true").lower() != "false":
        raise TrialHalted("AGENT_LIVE_KILL_SWITCH must explicitly be false")
    if any(_enabled(name) for name in ("AUTO_BUY_LIVE", "AUTO_SELL_LIVE", "AUTO_REBUY_ENABLED")):
        raise TrialHalted("turn AUTO_BUY_LIVE, AUTO_SELL_LIVE and AUTO_REBUY_ENABLED off for exclusive trial ownership")
    if _enabled("AGENT_LIVE_CANARY_ONLY"):
        raise TrialHalted("AGENT_LIVE_CANARY_ONLY must be false for the multi-agent trial")


def _read_recommendations() -> dict:
    return _read_json(os.getenv("RECOMMENDATION_SNAPSHOT_PATH", "launch_guard_recommendations.json"))


def _read_portfolio() -> dict:
    return _read_json(os.getenv("PORTFOLIO_SNAPSHOT_PATH", "launch_guard_portfolio.json"))


def _write_report(ledger: LiveTrialLedger) -> None:
    path = Path(os.getenv("AGENT_LIVE_TRIAL_REPORT_PATH", "launch_guard_live_trial_report.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(ledger.report(), handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def _signal_decision(snapshot: dict, mint: str) -> str | None:
    """The fresh sell-worthy decision for `mint`, or None if not eligible.

    Checks `raw_decision` (the risk-based call before PortfolioAdvisor's
    dollar-value floor override), not the notification-facing `decision` -
    otherwise a position that crashes below the sell minimum reads as HOLD
    forever and can never be liquidated, even though the true underlying
    signal is bearish. Falls back to `decision` for an older snapshot that
    predates this field.
    """
    if not _snapshot_is_fresh(snapshot):
        return None
    signals = snapshot.get("signals")
    if not isinstance(signals, list):
        return None
    for row in signals:
        if not isinstance(row, dict) or row.get("chain") != "solana" or row.get("token_address") != mint:
            continue
        effective_decision = row.get("raw_decision", row.get("decision"))
        if effective_decision not in SELL_WORTHY_DECISIONS:
            continue
        try:
            price = float(row.get("current_price") or 0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and price > 0:
            return effective_decision
    return None


def _eligible_exit(snapshot: dict, mint: str) -> bool:
    return _signal_decision(snapshot, mint) is not None


def _sell_choice(raw: dict, mint: str, value: float, signal: str,
                 max_partial_fraction: float = 1.0) -> tuple[str, float] | None:
    """Model cannot authorize a different mint, size, or unsupported exit."""
    try:
        action = TradeAction(str(raw["action"]).upper())
        confidence = float(raw["confidence"])
        requested = float(raw.get("requested_usd") or 0)
    except (ValueError, TypeError, KeyError):
        return None
    if (raw.get("mint") != mint or not 0.65 <= confidence <= 1
        or not math.isfinite(requested) or not math.isfinite(value)
        or requested > value * 1.01):
        return None
    normalized = signal.replace("_", " ")
    is_full_liquidation = (action is TradeAction.SELL
                           and normalized in {"EXIT WARNING", "EXIT", "EMERGENCY EXIT"}
                           and abs(requested - value) <= max(.05, .01 * value))
    # A full exit liquidates whatever remains, so it isn't floored at the
    # usual $2 minimum trade size - a position that crashed below that
    # floor still needs to be sellable. A discretionary partial sell has
    # no such justification for going below $2: it can just wait.
    if requested < (0.01 if is_full_liquidation else 2):
        return None
    if is_full_liquidation:
        return "SELL", 1.0
    if (action is TradeAction.TAKE_PARTIAL and normalized in {"TAKE PARTIAL", "PROTECT PROFIT"}
        and 0 < requested / value <= max_partial_fraction + 0.0001):
        return "TAKE_PARTIAL", requested / value
    return None


async def _notify(settings: Settings, *, title: str, message: str) -> None:
    if not settings.pushover_app_token or not settings.pushover_user_key:
        return
    from .notifications import PushoverClient
    try:
        await PushoverClient(app_token=settings.pushover_app_token,
                             user_key=settings.pushover_user_key,
                             device=settings.pushover_device).send(
                                 title=title, message=message, priority=1)
    except (OSError, ValueError, ConnectionError) as exc:
        print(f"Notification could not be delivered: {exc}", flush=True)


async def _guarded_entry(decision: LiveEntryDecision, *, ledger: LiveTrialLedger, **kwargs) -> dict | None:
    """Deterministic, pre-reservation rejections (a quote's price impact or
    slippage exceeding the guarded limit, most often on a fast-moving
    candidate) are routine and market-condition-dependent - they can pass on
    a later cycle once a fresh quote is in, so this skips just this attempt
    and retries next cycle instead of the exception reaching supervise()'s
    generic handler, which can't tell a buy failure from a sell failure and
    always mislabels it agent="portfolio-v1". Anything past reservation is
    already handled and logged by execute_hunter_entry's own except block, so
    only pre-reservation ValueErrors (and a preflight TransactionSimulationFailed,
    which never reaches Jupiter's own retryable-vs-fatal distinction - it's
    a bare on-chain revert during simulation, most often slippage, equally
    routine) are handled here - re-raise anything else, same as
    _guarded_exit does for the sell side.
    """
    try:
        return await execute_hunter_entry(decision, ledger=ledger, **kwargs)
    except AdditionalSignerError as exc:
        raise TrialHalted("buy signing failure; stop new buys and inspect wallet") from exc
    except (ValueError, TransactionSimulationFailed) as exc:
        if ledger.unresolved():
            raise
        ledger.assert_active()
        ledger.log(agent="hunter-v1", mint=decision.mint, state="BUY_BLOCKED",
                   reason=f"entry rejected before broadcast: {str(exc)[:400]}")
        return None


async def _guarded_exit(*, ledger: LiveTrialLedger, agent: str, mint: str,
                        requested_usd: float, **kwargs) -> dict | None:
    try:
        return await execute_live_exit(ledger=ledger, agent=agent, mint=mint, **kwargs)
    except AdditionalSignerError as exc:
        raise TrialHalted("sell signing failure; stop new buys and inspect wallet") from exc
    except (ValueError, ConnectionError, TrialHalted, TransactionSimulationFailed) as exc:
        if ledger.unresolved() or (isinstance(exc, TrialHalted) and not str(exc).startswith("EXIT BLOCKED")):
            raise
        ledger.assert_active()
        ledger.log(agent=agent, mint=mint, state="EXIT_BLOCKED",
                   reason=f"value {requested_usd:.2f} USD; {str(exc)[:400]}")
        return None


EXIT_STUCK_ALERT_STREAK = 3


async def _track_exit_block_streak(
    settings: Settings, exit_block_streaks: dict[str, int], *,
    owner: str, mint: str, blocked: bool,
) -> None:
    """Alert once a mint has failed to exit for several consecutive cycles.

    A single blocked exit is routine (a stale quote, a changed signal) and
    just retries next cycle without stopping the trial. But retrying
    silently forever would mean a position that's genuinely stuck could sit
    unexited for hours with nothing on the operator's phone - unlike the
    old behavior, which always halted (and always notified) on the first
    block. This keeps the "don't halt" fix without losing that visibility.
    """
    if not blocked:
        exit_block_streaks.pop(mint, None)
        return
    streak = exit_block_streaks.get(mint, 0) + 1
    exit_block_streaks[mint] = streak
    if streak == EXIT_STUCK_ALERT_STREAK:
        await _notify(
            settings, title="Launch Guard EXIT STUCK",
            message=f"{owner}: {mint} has failed to exit for "
                    f"{streak} consecutive cycles; still retrying "
                    "automatically, not halted.",
        )


async def cycle(*, ledger: LiveTrialLedger, settings: Settings, rpc: SolanaRpc,
                model, store: SQLiteStore,
                oracle: DexScreenerOracle,
                exit_block_streaks: dict[str, int],
                buy_zone_skip_reasons: dict[str, str],
                chase_first_target: dict[str, float],
                regrowth_skip_reasons: dict[str, str],
                regrowth_confirmation_counts: dict[str, int]) -> None:
    ledger.assert_active()
    if ledger.unresolved():
        raise TrialHalted("unresolved order; stop for chain reconciliation")
    wallet = settings.solana_wallet_address
    if not wallet or not settings.jupiter_api_key:
        raise TrialHalted("Solana wallet and Jupiter API key are required")
    try:
        signer = KeyringSolanaSigner(expected_public_key=wallet)
    except (ValueError, RuntimeError) as exc:
        raise TrialHalted("wallet signer unavailable; stop new buys") from exc
    client = JupiterSwapClient(api_key=settings.jupiter_api_key)
    buyer = SolanaAutoBuyer(client=client, signer=signer,
                            max_price_impact_pct=min(9, settings.auto_buy_max_price_impact_pct),
                            max_slippage_bps=min(1700, settings.auto_buy_max_slippage_bps))
    momentum_buyer = SolanaAutoBuyer(client=client, signer=signer,
                                     max_price_impact_pct=settings.momentum_buy_max_price_impact_pct,
                                     max_slippage_bps=settings.momentum_buy_max_slippage_bps)
    seller = SolanaAutoSeller(client=client, signer=signer,
                              max_price_impact_pct=min(8, settings.auto_sell_max_price_impact_pct),
                              max_slippage_bps=min(1500, settings.auto_sell_max_slippage_bps))
    emergency_seller = SolanaAutoSeller(client=client, signer=signer,
                                        max_price_impact_pct=settings.emergency_sell_max_price_impact_pct,
                                        max_slippage_bps=settings.emergency_sell_max_slippage_bps)
    portfolio = _read_portfolio()
    if _snapshot_is_fresh(portfolio):
        for row in portfolio.get("signals", []):
            if not isinstance(row, dict) or row.get("chain") != "solana":
                continue
            signal = str(row.get("raw_decision", row.get("decision")) or "")
            if signal not in SELL_WORTHY_DECISIONS:
                continue
            mint = str(row.get("token_address") or "")
            try:
                value = float(row.get("current_value_usd") or 0)
                liquidity = float(row.get("liquidity_usd") or 0)
            except (ValueError, TypeError):
                continue
            # EXIT WARNING liquidates the entire remaining position, so a
            # value below the usual $2 floor must not block it - otherwise a
            # position that crashes below the floor could never be sold
            # again. TAKE PARTIAL / PROTECT PROFIT choose a discretionary
            # size, so they keep the $2 floor: no reason to bother with a
            # partial sell that small while the rest isn't going anywhere.
            value_floor = 0.01 if signal == "EXIT WARNING" else 2
            if not math.isfinite(value) or value < value_floor or not math.isfinite(liquidity) or liquidity <= 0:
                continue
            if not store.is_launch_guard_owned(chain="solana", token_address=mint):
                classification = store.classify_external_wallet_position(
                    mint, current_value_usd=value,
                    known_purchase_ranges_usd=COPYFOMO_PURCHASE_RANGES_USD,
                )
                if classification == "external_known":
                    continue
                # "external_personal" (or any future non-CopyFomo external
                # source) still gets managed normally below - only a
                # recognized CopyFomo-sized buy is deliberately left alone.
            try:
                pnl_pct = float(row["pnl_pct"]) if row.get("pnl_pct") is not None else None
            except (TypeError, ValueError):
                pnl_pct = None
            emergency = _should_escalate_to_emergency(
                pnl_pct=pnl_pct, block_streak=exit_block_streaks.get(mint, 0),
            )
            if emergency:
                # Deterministic: no model round trip, no confidence gate -
                # the risk engine has final authority once a capital-
                # preservation condition is crossed. Always a full exit.
                thesis = (f"deterministic emergency escalation: pnl {pnl_pct:.1f}% "
                          f"block streak {exit_block_streaks.get(mint, 0)}" if pnl_pct is not None
                          else "deterministic emergency escalation")
                ledger.log(agent="portfolio-v1", mint=mint, state="EMERGENCY_ESCALATION", reason=thesis)
                chosen = ("SELL", 1.0)
                active_seller = emergency_seller
                active_max_impact = settings.emergency_sell_max_price_impact_pct
                active_max_slippage = settings.emergency_sell_max_slippage_bps
            elif _exit_warning_model_call_is_redundant(signal, exit_block_streaks.get(mint, 0)):
                thesis = (f"deterministic: EXIT WARNING for {exit_block_streaks[mint]} "
                          "consecutive blocked cycles with no cost basis to evaluate - "
                          "repeating the model's already-consistent full-exit answer")
                ledger.log(agent="portfolio-v1", mint=mint, state="PROPOSAL", reason=thesis)
                chosen = ("SELL", 1.0)
                active_seller = seller
                active_max_impact = min(8, settings.auto_sell_max_price_impact_pct)
                active_max_slippage = min(1500, settings.auto_sell_max_slippage_bps)
            else:
                raw = model.propose(role=AgentRole.PORTFOLIO_MANAGER,
                    context={"mode": "live_trial", "owned_position": row,
                             "constraint": "Decide only a full SELL for EXIT WARNING or a TAKE_PARTIAL for TAKE PARTIAL / PROTECT PROFIT; HOLD is permitted. Never buy."})
                thesis = str(raw.get("thesis") or "HOLD")
                ledger.log(agent="portfolio-v1", mint=mint, state="PROPOSAL", reason=thesis)
                permitted_fraction = (settings.auto_sell_take_partial_fraction if signal == "TAKE PARTIAL"
                                      else settings.auto_sell_protect_profit_fraction)
                sell_choice = _sell_choice(raw, mint, value, signal, permitted_fraction)
                if sell_choice is None:
                    continue
                chosen = sell_choice
                active_seller = seller
                active_max_impact = min(8, settings.auto_sell_max_price_impact_pct)
                active_max_slippage = min(1500, settings.auto_sell_max_slippage_bps)
            owner = next((p["agent"] for p in ledger.positions() if p["mint"] == mint), "portfolio-v1")
            result = await _guarded_exit(
                ledger=ledger, agent=owner, mint=mint, requested_usd=value * chosen[1],
                symbol=str(row.get("symbol") or mint[:8]),
                decision=chosen[0], fraction=chosen[1],
                position_value_usd=value, quote_age_seconds=time.time() - float(portfolio["generated_at"]),
                liquidity_usd=liquidity, rpc=rpc, seller=active_seller, store=store, wallet=wallet,
                minimum_sell_usd=max(settings.portfolio_min_sell_value_usd,
                                     settings.auto_sell_min_value_usd),
                max_price_impact_pct=active_max_impact,
                max_slippage_bps=active_max_slippage,
                current_exit_allowed=lambda mint=mint: _eligible_exit(_read_portfolio(), mint),
                current_decision=lambda mint=mint: _signal_decision(_read_portfolio(), mint),
            )
            await _track_exit_block_streak(
                settings, exit_block_streaks, owner=owner, mint=mint,
                blocked=result is None,
            )
            if result is None:
                continue
            await _notify(settings, title=f"Launch Guard {chosen[0]}",
                          message=f"{owner}: {mint} chain-confirmed; {result['proceeds_usdc_raw'] / 1_000_000:.2f} USDC; {thesis[:100]}")
            # Do not risk another order from a snapshot made before this sale.
            return
    unpriced_position = False
    for position in ledger.positions():
        quote = await oracle.quote(position["mint"])
        if not quote or not quote.price_usd or not quote.liquidity_usd:
            unpriced_position = True
            ledger.log(agent=position["agent"], mint=position["mint"],
                       state="HOLD", reason="fresh USD quote unavailable")
            continue
        marked = ledger.mark_position(agent=position["agent"], mint=position["mint"],
                                      price=quote.price_usd)
        market = {"price": quote.price_usd, "liquidity_usd": quote.liquidity_usd,
                  "price_change_m5_pct": quote.price_change_m5_pct,
                  "buys_m5": quote.buys_m5, "sells_m5": quote.sells_m5}
        review = assess_exit({"entry_price": marked["entry_price"],
                              "highest_price_since_entry": marked["peak_price"],
                              "entry_liquidity_usd": marked["entry_liquidity_usd"],
                              "principal_recovered": bool(marked["principal_recovered"]),
                              "second_stage_taken": bool(marked["second_stage_taken"]),
                              "opened_at": marked["opened_at"]},
                             market, ShadowRecoveryPolicy.from_env())
        ledger.log(agent=position["agent"], mint=position["mint"],
                   state=review["state"], reason="; ".join(review["reasons"]))
        if review["state"] not in {"EXIT", "EMERGENCY_EXIT", "TAKE_PARTIAL"}:
            continue
        current_value = quote.price_usd * marked["quantity_raw"] / 10**marked["decimals"]
        gain_pct = ((quote.price_usd / marked["entry_price"] - 1) * 100
                   if marked["entry_price"] > 0 else None)
        stage_key = ""
        partial_limit = 1.0
        emergency = False
        if review["state"] == "TAKE_PARTIAL":
            if not marked["principal_recovered"]:
                stage_key = "PRINCIPAL"
                partial_limit = min(1.0, marked["cost_cents"] / 100 / current_value)
            elif not marked["second_stage_taken"]:
                stage_key = "SECOND_STAGE"
                partial_limit = ShadowRecoveryPolicy.from_env().second_stage_fraction
            else:
                continue
        else:
            emergency = _should_escalate_to_emergency(
                pnl_pct=gain_pct, block_streak=exit_block_streaks.get(position["mint"], 0),
                liquidity_collapse=(review["state"] == "EMERGENCY_EXIT"),
            )
        if emergency:
            ledger.log(agent=position["agent"], mint=position["mint"],
                       state="EMERGENCY_ESCALATION", reason="; ".join(review["reasons"]))
            chosen = ("SELL", 1.0)
            active_seller = emergency_seller
            active_max_impact = settings.emergency_sell_max_price_impact_pct
            active_max_slippage = settings.emergency_sell_max_slippage_bps
        else:
            raw = model.propose(role=AgentRole.PORTFOLIO_MANAGER,
                context={"mode": "live_trial", "owned_position": marked,
                         "fresh_quote": market, "reversal_review": review,
                         "constraint": "Choose SELL for EXIT/EMERGENCY_EXIT or TAKE_PARTIAL for TAKE_PARTIAL, or HOLD. No buys."})
            sell_choice = _sell_choice(raw, position["mint"], current_value, review["state"], partial_limit)
            if sell_choice is None:
                continue
            chosen = sell_choice
            active_seller = seller
            active_max_impact = min(8, settings.auto_sell_max_price_impact_pct)
            active_max_slippage = min(1500, settings.auto_sell_max_slippage_bps)
        def fresh_exit() -> bool:
            # Sells need a new oracle quote in cycle; flag and ledger enforce
            # stop/deadline, while the seller creates another fresh market quote.
            return (time.time() - marked["updated_at"] <= 15)
        result = await _guarded_exit(
            ledger=ledger, agent=position["agent"], mint=position["mint"],
            requested_usd=current_value * chosen[1],
            stage_key=stage_key,
            symbol=quote.symbol, decision=chosen[0], fraction=chosen[1],
            position_value_usd=current_value, quote_age_seconds=time.time() - marked["updated_at"],
            liquidity_usd=quote.liquidity_usd, rpc=rpc, seller=active_seller, store=store,
            wallet=wallet, current_exit_allowed=fresh_exit,
            minimum_sell_usd=max(settings.portfolio_min_sell_value_usd,
                                 settings.auto_sell_min_value_usd),
            max_price_impact_pct=active_max_impact,
            max_slippage_bps=active_max_slippage,
        )
        await _track_exit_block_streak(
            settings, exit_block_streaks, owner=position["agent"],
            mint=position["mint"], blocked=result is None,
        )
        if result is None:
            continue
        await _notify(settings, title=f"Launch Guard {review['state']}",
                      message=f"{position['agent']}: {position['mint']} chain-confirmed; {result['proceeds_usdc_raw']/1_000_000:.2f} USDC; {'; '.join(review['reasons'])[:100]}")
        return
    if unpriced_position or not _snapshot_is_fresh(portfolio):
        # These are reported together in one gate because both mean "system
        # health is uncertain, don't risk a new buy" - but the reason text
        # names the one that actually applied instead of always naming both,
        # so "why isn't it buying" is answerable without reading the code.
        reason = ("an owned position's fresh USD quote is unavailable" if unpriced_position
                  else "portfolio monitor snapshot is stale")
        ledger.log(agent="hunter-v1", mint="", state="BUY_BLOCKED", reason=reason)
        return
    recommendations = _read_recommendations()
    decision = decide_hunter_entry(recommendations, model=model, ledger=ledger,
                                   buy_zone_skip_reasons=buy_zone_skip_reasons,
                                   chase_first_target=chase_first_target,
                                   current_snapshot=_read_recommendations)
    if decision is not None:
        active_buyer = (momentum_buyer if decision.candidate.get("decision") == "MOMENTUM BUY"
                       else buyer)
        result = await _guarded_entry(decision, ledger=ledger, rpc=rpc,
                                      buyer=active_buyer, store=store, wallet=wallet,
                                      current_snapshot=_read_recommendations)
        if result is None:
            return
        await _notify(settings, title="Launch Guard CONFIRMED BUY",
                      message=f"hunter-v1: {result['mint']} spent {result['spent_cents']/100:.2f} USD; {decision.reason[:100]}; {result['signature']}")
        return
    # A mint hunter-v1 already fully exited doesn't get forgotten just
    # because it fell off the shared recommendation engine's tracking pool -
    # this is a completely separate signal and budget from the fresh-
    # candidate path above (see decide_regrowth_rebuy), so it's checked
    # every cycle the normal path didn't already act, not only when it's
    # empty.
    regrowth_decision = await decide_regrowth_rebuy(model=model, ledger=ledger, oracle=oracle,
                                                     regrowth_skip_reasons=regrowth_skip_reasons,
                                                     regrowth_confirmation_counts=regrowth_confirmation_counts)
    if regrowth_decision is not None:
        exit_price = regrowth_decision.candidate["regrowth_exit_price"]
        regrowth_mint = regrowth_decision.mint

        async def _still_growing(mint=regrowth_mint, exit_price=exit_price) -> bool:
            return _regrowth_bar_clears(await oracle.quote(mint), exit_price)

        result = await _guarded_entry(regrowth_decision, ledger=ledger, rpc=rpc,
                                      buyer=buyer, store=store, wallet=wallet,
                                      current_snapshot=_read_recommendations,
                                      final_eligibility_check=_still_growing,
                                      origin="regrowth")
        if result is None:
            return
        await _notify(settings, title="Launch Guard CONFIRMED BUY",
                      message=f"hunter-v1: {result['mint']} spent {result['spent_cents']/100:.2f} USD "
                              f"(regrowth re-entry); {regrowth_decision.reason[:100]}; {result['signature']}")


async def supervise(*, interval_seconds: int = 30) -> dict:
    if interval_seconds < 15:
        raise ValueError("live poll must be at least 15 seconds")
    require_exclusive_trial_flags()
    settings = Settings.from_env()
    if not settings.solana_wallet_address or not settings.jupiter_api_key:
        raise TrialHalted("Solana wallet and Jupiter API key are required")
    if not settings.pushover_app_token or not settings.pushover_user_key:
        raise TrialHalted("confirmed-trade and halt notifications require configured Pushover")
    KeyringSolanaSigner(expected_public_key=settings.solana_wallet_address)
    from .openai_agents import OpenAIProposalModel
    proposal_model = OpenAIProposalModel()
    proposal_model.client = proposal_model.client.with_options(max_retries=0)
    # One trial process owns the local data producer. Existing monitors must be
    # stopped by the operator before this is started, to avoid parallel sells.
    process_list = subprocess.check_output(["ps", "-axo", "command"], text=True)
    if any(f"-m solana_launch_guard.{name}" in process_list for name in
           ("app", "overnight_trial", "robinhood_agent_sell")):
        raise TrialHalted("another Launch Guard monitor or trading trial is running; stop it before starting")
    ledger = LiveTrialLedger(os.getenv("AGENT_LIVE_TRIAL_LEDGER_PATH", "launch_guard_live_trial.sqlite"))
    store = SQLiteStore(settings.database_path)
    monitor = None
    keep_read_only_monitor = False
    try:
        ledger.start()
        model = BoundedTrialModel(proposal_model, ledger)
        monitor_env = os.environ.copy()
        monitor_env.update(AUTO_BUY_LIVE="false", AUTO_SELL_LIVE="false",
                           AUTO_REBUY_ENABLED="false", AGENT_LIVE_CANARY_ONLY="false")
        monitor = subprocess.Popen([sys.executable, "-m", "solana_launch_guard.app",
                                    "--mode", "launches", "--portfolio-window"], env=monitor_env)
        rpc = SolanaRpc(settings.solana_rpc_http_url)
        oracle = DexScreenerOracle()
        consecutive_cycle_failures = 0
        exit_block_streaks: dict[str, int] = {}
        buy_zone_skip_reasons: dict[str, str] = {}
        chase_first_target: dict[str, float] = {}
        regrowth_skip_reasons: dict[str, str] = {}
        regrowth_confirmation_counts: dict[str, int] = {}
        while ledger.status()["status"] == "ACTIVE":
            require_exclusive_trial_flags()
            if monitor.poll() is not None:
                raise TrialHalted("monitor stopped; trial halted")
            try:
                await cycle(ledger=ledger, settings=settings, rpc=rpc,
                            model=model, store=store, oracle=oracle,
                            exit_block_streaks=exit_block_streaks,
                            buy_zone_skip_reasons=buy_zone_skip_reasons,
                            chase_first_target=chase_first_target,
                            regrowth_skip_reasons=regrowth_skip_reasons,
                            regrowth_confirmation_counts=regrowth_confirmation_counts)
                consecutive_cycle_failures = 0
            except TrialHalted:
                raise
            except (ValueError, ConnectionError, APIError) as exc:
                ledger.log(agent="portfolio-v1", mint="", state="CYCLE_BLOCKED", reason=str(exc))
                if ledger.unresolved():
                    raise TrialHalted("unresolved order after failed cycle; reconcile on chain") from exc
                consecutive_cycle_failures += 1
                if consecutive_cycle_failures >= 3:
                    raise TrialHalted("three consecutive failed cycles; stop live trial") from exc
            await asyncio.sleep(interval_seconds)
        keep_read_only_monitor = ledger.status()["status"] == "EXPIRED"
        return ledger.status()
    except BaseException as exc:
        ledger.halt(f"supervisor stopped: {type(exc).__name__}")
        pending = ledger.unresolved()
        concern = (f"{pending[0]['agent']} {pending[0]['mint']}: " if pending else "")
        await _notify(settings, title="Launch Guard LIVE HALTED",
                      message=(concern + str(exc))[:240])
        raise
    finally:
        try:
            _write_report(ledger)
        except (OSError, ValueError) as exc:
            print(f"Trial report could not be written: {exc}", flush=True)
        if monitor is not None and monitor.poll() is None and not keep_read_only_monitor:
            monitor.terminate()
            try:
                monitor.wait(timeout=10)
            except subprocess.TimeoutExpired:
                monitor.kill()
                monitor.wait()
        store.close()
        ledger.close()


async def preflight(*, sell_mint: str, sell_decision: str = "SELL") -> dict:
    """Simulate a $5 buy and a real owned-token exit without broadcasting."""
    from .app import preflight_owned_auto_sell
    from .live_trial_runner import SOLANA_ADDRESS

    if not SOLANA_ADDRESS.fullmatch(sell_mint):
        raise ValueError("--sell-mint must be a real public Solana mint")
    settings = Settings.from_env()
    if not settings.solana_wallet_address or not settings.jupiter_api_key:
        raise ValueError("Solana wallet and Jupiter API key are required")
    signer = KeyringSolanaSigner(expected_public_key=settings.solana_wallet_address)
    rpc = SolanaRpc(settings.solana_rpc_http_url)
    balance = await asyncio.to_thread(rpc._request, "getBalance", [signer.public_key])
    sol_lamports = int(balance["value"])
    if sol_lamports < 10_000_000:
        raise TrialHalted("SOL balance below 0.01; sponsored quotes cannot be fully verified here")
    usdc = await rpc.token_balance(signer.public_key, USDC_MINT)
    if usdc.raw_amount < 5_000_000:
        raise TrialHalted("wallet has less than $5 USDC for the trial buy simulation")
    buyer = SolanaAutoBuyer(
        client=JupiterSwapClient(api_key=settings.jupiter_api_key), signer=signer,
        max_price_impact_pct=min(9, settings.auto_buy_max_price_impact_pct),
        max_slippage_bps=min(1700, settings.auto_buy_max_slippage_bps),
    )
    buy = await buyer.preflight(BuyIntent(
        mint=WRAPPED_SOL_MINT, symbol="WSOL", event_key="live-trial:preflight-only",
        amount_usdc_raw=5_000_000, funding_source="preflight-only",
    ), rpc)
    if buy.prepared.input_amount_raw != 5_000_000:
        raise TrialHalted("buy simulation did not preserve the $5 input")
    store = SQLiteStore(settings.database_path)
    try:
        sell = await preflight_owned_auto_sell(settings, store, sell_mint, sell_decision)
    finally:
        store.close()
    return {
        "result": "PASSED", "broadcast": False, "wallet": signer.public_key,
        "native_sol": sol_lamports / 1_000_000_000,
        "buy_simulation": {"pair": "USDC/WSOL", "amount_usdc": 5,
                           "router": buy.prepared.router,
                           "simulation_units": buy.units_consumed},
        "sell_simulation": sell,
        "note": "The buy simulation checks signing and routing; it does not prove a future candidate has a safe exit.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--start", action="store_true")
    group.add_argument("--preflight", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--sell-mint")
    parser.add_argument("--sell-decision", choices=("SELL", "TAKE_PARTIAL"), default="SELL")
    args = parser.parse_args()
    if args.start and args.confirm != "START_ONE_EIGHT_HOUR_SOLANA_TRIAL":
        raise SystemExit("explicit trial confirmation is required")
    if args.preflight and not args.sell_mint:
        raise SystemExit("preflight requires --sell-mint for an owned-token sell simulation")
    _load_dotenv()
    try:
        result = asyncio.run(supervise()) if args.start else asyncio.run(
            preflight(sell_mint=args.sell_mint, sell_decision=args.sell_decision)
        )
        print(json.dumps(result, indent=2))
    except (ValueError, RuntimeError, OSError) as exc:
        raise SystemExit(f"Live trial stopped: {exc}") from None


if __name__ == "__main__":
    main()
