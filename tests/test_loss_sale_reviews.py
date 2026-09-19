import pytest

from solana_launch_guard.core import SQLiteStore
from solana_launch_guard.rebuy_assessment import auto_rebuy_recovery_assessment
from solana_launch_guard.config import Settings
from solana_launch_guard.market import MarketQuote


def test_only_verified_net_losses_enter_rebound_review(tmp_path):
    store = SQLiteStore(str(tmp_path / "history.sqlite"))
    with pytest.raises(ValueError, match="net-loss"):
        store.record_loss_sale(sale_id="profit", token_address="MINT",
                               symbol="TOKEN", cost_usd=2, proceeds_usd=3,
                               quantity=1, sold_at_epoch=100)
    row = store.record_loss_sale(sale_id="tx1", token_address="MINT",
                                 symbol="TOKEN", cost_usd=5,
                                 proceeds_usd=3, quantity=3,
                                 sold_at_epoch=100)
    assert row["lowest_price_usd"] == 1
    assert len(store.loss_sale_reviews()) == 1
    assert store.record_loss_sale(sale_id="tx1", token_address="MINT",
                                  symbol="TOKEN", cost_usd=5,
                                  proceeds_usd=3, quantity=3,
                                  sold_at_epoch=100)["sale_id"] == "tx1"
    with pytest.raises(ValueError, match="different amounts"):
        store.record_loss_sale(sale_id="tx1", token_address="MINT",
                               symbol="TOKEN", cost_usd=6,
                               proceeds_usd=3, quantity=3,
                               sold_at_epoch=100)
    store.update_loss_sale_review("tx1", price_usd=0.8, qualified=True,
                                  reason="first", confirmation_required=2)
    ready = store.update_loss_sale_review("tx1", price_usd=0.9,
                                          qualified=True, reason="second",
                                          confirmation_required=2)
    assert ready["decision"] == "REBUY REVIEW"
    store.reset_loss_sale_review("tx1", "quote unavailable")
    reset = store.loss_sale_reviews()[0]
    assert reset["decision"] == "REBUY WATCH"
    assert reset["confirmation_count"] == 0


def test_old_loss_sale_can_be_reviewed_without_authorizing_auto_buy():
    settings = Settings.from_env()
    watch = {"sold_at_epoch": 100, "exit_price_usd": 1.0,
             "lowest_price_usd": 0.70, "last_price_usd": 0.74,
             "exit_liquidity_usd": 60_000}
    quote = MarketQuote(mint="MINT", symbol="TEST", price_sol=0.01,
                        price_usd=0.8, chain="solana", liquidity_usd=60_000,
                        market_cap_usd=100_000, pair_address="Pair",
                        pair_created_at_ms=1, buys_m5=20, sells_m5=8,
                        volume_m5_usd=3_000, price_change_m5_pct=4)
    assert not auto_rebuy_recovery_assessment(watch, quote, settings,
                                              now=200_000)[0]
    qualified, reason, _ = auto_rebuy_recovery_assessment(
        watch, quote, settings, now=200_000, advisory=True
    )
    assert qualified and "recovery confirmed" in reason
