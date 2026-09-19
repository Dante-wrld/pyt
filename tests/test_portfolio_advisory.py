from solana_launch_guard.market import MarketQuote
from solana_launch_guard.portfolio import (
    OwnedHolding, PortfolioAdvisor, build_portfolio_snapshot,
    format_portfolio_dashboard,
)
from solana_launch_guard.notifications import format_portfolio_notification
from solana_launch_guard.notifications import PortfolioNotifier
from dataclasses import replace
import asyncio


def _quote(**changes):
    values = dict(
        mint="Mint111", symbol="TEST", price_sol=0.01,
        price_usd=9.5, chain="solana", liquidity_usd=60_000,
        market_cap_usd=100_000, pair_address="Pair111",
        pair_created_at_ms=1, buys_m5=20, sells_m5=8,
        volume_m5_usd=3_000, price_change_m5_pct=2,
    )
    values.update(changes)
    return MarketQuote(**values)


def _holding(entry_price=10):
    return OwnedHolding("solana", "Mint111", "TEST", 1, entry_price, "USD")


def test_buy_more_requires_cost_basis_and_confirmation():
    assert PortfolioAdvisor().evaluate(_holding(), _quote()).decision == "BUY MORE"
    assert PortfolioAdvisor().evaluate(_holding(None), _quote()).decision == "HOLD"
    assert PortfolioAdvisor().evaluate(_holding(), _quote(buys_m5=5)).decision == "HOLD"
    assert PortfolioAdvisor().evaluate(_holding(), _quote(liquidity_usd=4_000)).decision == "HOLD"
    assert PortfolioAdvisor().evaluate(_holding(), _quote(price_change_m5_pct=-2)).decision == "HOLD"


def test_sell_risk_takes_precedence_over_buy_more():
    assert PortfolioAdvisor().evaluate(_holding(), _quote(price_usd=7)).decision == "EXIT WARNING"


def test_buy_more_and_hold_render_with_advisory_alerts():
    buy = PortfolioAdvisor().evaluate(_holding(), _quote())
    hold = PortfolioAdvisor().evaluate(_holding(None), _quote())
    dashboard = format_portfolio_dashboard(
        build_portfolio_snapshot([buy, hold], wallet=None, poll_seconds=15),
        color=False,
    )
    assert "BUY MORE" in dashboard and "HOLD" in dashboard
    assert "Advisory only" in format_portfolio_notification(buy)[1]
    assert "HOLD POSITION" in format_portfolio_notification(hold)[0]


def test_sub_two_dollar_holding_never_suggests_a_sell():
    advisor = PortfolioAdvisor()
    tiny = OwnedHolding("solana", "Mint111", "TEST", 1, 3, "USD")
    signal = advisor.evaluate(tiny, _quote(price_usd=1.90, price_change_m5_pct=-12,
                                           buys_m5=2, sells_m5=9))
    assert signal.decision == "HOLD"
    assert "sell minimum" in signal.reason
    assert advisor.below_sell_minimum_for("solana", "Mint111")


def test_recovery_across_sell_floor_requires_two_bearish_signals():
    advisor = PortfolioAdvisor()
    holding = OwnedHolding("solana", "Mint111", "TEST", 1, 3, "USD")
    advisor.evaluate(holding, _quote(price_usd=1.90))
    # A stop-loss alone cannot predict that another rise is unlikely.
    rebound = advisor.evaluate(holding, _quote(price_usd=2.20, buys_m5=20,
                                               sells_m5=1, price_change_m5_pct=2))
    assert rebound.decision == "HOLD"
    # Momentum reversal together with a large drawdown provides observable evidence.
    advisor.restore_state(chain="solana", token_address="Mint111", peak_price=3.0,
                          baseline_liquidity_usd=60_000, below_sell_minimum=True)
    bearish = advisor.evaluate(holding, _quote(price_usd=2.20, buys_m5=2,
                                               sells_m5=9, price_change_m5_pct=-10))
    assert bearish.decision == "EXIT WARNING"


def test_partial_sell_below_two_dollars_is_not_recommended():
    holding = OwnedHolding("solana", "Mint111", "TEST", 1, 2, "USD")
    signal = PortfolioAdvisor().evaluate(holding, _quote(price_usd=3.0))
    assert signal.decision == "HOLD"
    assert "partial sell" in signal.reason


def test_phone_alert_suppressed_below_floor_even_for_exit_warning():
    class Store:
        def last_notification(self, *args):
            return None
        def save_notification(self, **kwargs):
            pass
    class Client:
        def __init__(self):
            self.calls = 0
        async def send(self, **kwargs):
            self.calls += 1
    client = Client()
    notifier = PortfolioNotifier(client=client, store=Store(),
                                 decisions=("EXIT WARNING",),
                                 high_priority_decisions=("EXIT WARNING",),
                                 cooldown_seconds=300)
    signal = PortfolioAdvisor().evaluate(_holding(), _quote(price_usd=7))
    assert asyncio.run(notifier.maybe_send(replace(signal, current_value_usd=1.99))) is False
    assert client.calls == 0


def test_compact_holdings_screen_keeps_complete_snapshot():
    priced = PortfolioAdvisor().evaluate(_holding(), _quote(price_usd=9.5))
    small = PortfolioAdvisor().evaluate(
        OwnedHolding("solana", "Mint222", "SMALL", 0.1, None, "USD"),
        _quote(mint="Mint222", symbol="SMALL", price_usd=9.5),
    )
    unpriced = PortfolioAdvisor().evaluate(
        OwnedHolding("solana", "Mint333", "MISSING", 1), None
    )
    snapshot = build_portfolio_snapshot(
        [priced, small, unpriced], wallet=None, poll_seconds=15,
        loss_sale_reviews=[{"sale_id": "sale1", "decision": "REBUY WATCH"}],
    )
    assert len(snapshot["signals"]) == 3
    assert len(snapshot["loss_sale_reviews"]) == 1
    compact = format_portfolio_dashboard(snapshot, color=False)
    complete = format_portfolio_dashboard(snapshot, color=False, show_all=True)
    assert "SMALL" not in compact and "MISSING" not in compact
    assert "SMALL" in complete and "MISSING" in complete
    assert "2 small or unpriced holdings hidden" in compact
