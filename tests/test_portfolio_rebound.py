from solana_launch_guard.market import MarketQuote
from solana_launch_guard.notifications import format_portfolio_notification
from solana_launch_guard.portfolio import (
    OwnedHolding, PortfolioAdvisor, build_portfolio_snapshot,
    format_portfolio_dashboard,
)


def quote(price, **overrides):
    data = dict(
        mint="MintRebound111", symbol="RB", price_sol=0.01,
        price_usd=price, liquidity_usd=60_000,
        market_cap_usd=100_000, pair_address="PairRebound111",
        pair_created_at_ms=1, buys_m5=20, sells_m5=8,
        volume_m5_usd=3_000, price_change_m5_pct=2,
    )
    data.update(overrides)
    return MarketQuote(**data)


def holding(basis=10):
    return OwnedHolding(
        "solana", "MintRebound111", "RB", 1, basis, "USD"
    )


def test_rebound_watch_requires_tracked_peak_and_positive_basis():
    advisor = PortfolioAdvisor()
    assert advisor.evaluate(holding(), quote(12.5)).decision == "HOLD"
    signal = advisor.evaluate(holding(), quote(11.5))
    assert signal.decision == "REBOUND WATCH"
    assert "8.0% below monitored peak" in signal.reason
    dashboard = format_portfolio_dashboard(
        build_portfolio_snapshot([signal], wallet=None, poll_seconds=15),
        color=False,
    )
    assert "REBOUND WATCH" in dashboard
    assert "Advisory only" in format_portfolio_notification(signal)[1]


def test_rebound_watch_rejects_weak_market_and_missing_basis():
    for override in (
        {"price_change_m5_pct": -1},
        {"buys_m5": 3},
        {"liquidity_usd": 3_000},
        {"volume_m5_usd": 100},
    ):
        advisor = PortfolioAdvisor()
        advisor.evaluate(holding(), quote(12.5))
        assert advisor.evaluate(holding(), quote(11.5, **override)).decision == "HOLD"
    advisor = PortfolioAdvisor()
    advisor.evaluate(holding(None), quote(12.5))
    assert advisor.evaluate(holding(None), quote(11.5)).decision == "HOLD"


def test_sell_protection_beats_rebound_watch():
    advisor = PortfolioAdvisor()
    advisor.evaluate(holding(), quote(15))
    assert advisor.evaluate(holding(), quote(13)).decision == "PROTECT PROFIT"
