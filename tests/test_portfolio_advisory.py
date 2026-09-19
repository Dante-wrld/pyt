from solana_launch_guard.market import MarketQuote
from solana_launch_guard.portfolio import (
    OwnedHolding, PortfolioAdvisor, build_portfolio_snapshot,
    format_portfolio_dashboard,
)
from solana_launch_guard.notifications import format_portfolio_notification


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
