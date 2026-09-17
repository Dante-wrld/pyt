from __future__ import annotations

from pathlib import Path

import pytest

from solana_launch_guard.config import Settings
from solana_launch_guard.intelligence import CoinIntelligence
from solana_launch_guard.market import MarketQuote
from solana_launch_guard.wallet import parse_wallet_trades

from solana_launch_guard.core import (
    Launch,
    PaperBroker,
    RiskEngine,
    SQLiteStore,
    indicative_price,
)


def settings(database_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "ws_url": "wss://example.invalid/api/data",
        "api_key": None,
        "trade_size_sol": 0.02,
        "max_open_positions": 3,
        "max_total_exposure_sol": 0.06,
        "min_virtual_sol": 5.0,
        "min_market_cap_sol": 5.0,
        "max_market_cap_sol": 500.0,
        "max_creator_buy_sol": 5.0,
        "take_profit_pct": 30.0,
        "stop_loss_pct": 20.0,
        "reject_unknown_price": True,
        "database_path": str(database_path),
        "log_level": "INFO",
    }
    values.update(overrides)
    result = Settings(**values)
    result.validate()
    return result


def launch_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "mint": "Mint111",
        "name": "Example",
        "symbol": "EX",
        "txType": "create",
        "traderPublicKey": "Creator111",
        "signature": "Signature111",
        "vTokensInBondingCurve": 1_000_000,
        "vSolInBondingCurve": 30,
        "marketCapSol": 30,
        "solAmount": 1,
    }
    payload.update(overrides)
    return payload


def test_indicative_price_uses_virtual_reserves() -> None:
    assert indicative_price(launch_payload()) == pytest.approx(0.00003)


def test_risk_engine_accepts_event_inside_limits(tmp_path: Path) -> None:
    config = settings(tmp_path / "test.db")
    store = SQLiteStore(config.database_path)
    broker = PaperBroker(config, store)

    decision = RiskEngine(config).evaluate(
        Launch.from_payload(launch_payload()), broker
    )

    assert decision.accepted is True
    assert decision.reasons == ()
    # The score cannot imply full verification because on-chain enrichment
    # is deliberately not present in version 1.
    assert decision.score < 100
    store.close()


def test_risk_engine_rejects_large_creator_buy(tmp_path: Path) -> None:
    config = settings(tmp_path / "test.db")
    store = SQLiteStore(config.database_path)
    broker = PaperBroker(config, store)

    decision = RiskEngine(config).evaluate(
        Launch.from_payload(launch_payload(solAmount=10)), broker
    )

    assert decision.accepted is False
    assert any("creator buy" in reason for reason in decision.reasons)
    store.close()


def test_paper_position_hits_take_profit_and_is_persisted(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path / "test.db")
    store = SQLiteStore(config.database_path)
    broker = PaperBroker(config, store)
    launch = Launch.from_payload(launch_payload())

    position = broker.open(launch)
    broker.mark(launch.mint, position.entry_price_sol * 1.31)

    assert position.status == "CLOSED"
    assert position.exit_reason == "TAKE_PROFIT"
    assert position.pnl_pct == pytest.approx(31.0)
    assert store.summary()["realized_pnl_sol"] > 0
    store.close()


def test_exposure_limit_rejects_next_position(tmp_path: Path) -> None:
    config = settings(
        tmp_path / "test.db",
        max_open_positions=5,
        max_total_exposure_sol=0.02,
    )
    store = SQLiteStore(config.database_path)
    broker = PaperBroker(config, store)
    first = Launch.from_payload(launch_payload(mint="MintOne"))
    second = Launch.from_payload(launch_payload(mint="MintTwo"))

    broker.open(first)
    decision = RiskEngine(config).evaluate(second, broker)

    assert decision.accepted is False
    assert "maximum total exposure would be exceeded" in decision.reasons
    store.close()


def test_wallet_transaction_parser_detects_token_buy() -> None:
    wallet = "Wallet1111111111111111111111111111111111111"
    mint = "Mint222222222222222222222222222222222222222"
    transaction = {
        "transaction": {
            "message": {
                "accountKeys": [{"pubkey": wallet}, {"pubkey": "Program111"}]
            }
        },
        "meta": {
            "preBalances": [2_000_000_000, 0],
            "postBalances": [1_899_995_000, 0],
            "preTokenBalances": [
                {
                    "owner": wallet,
                    "mint": mint,
                    "uiTokenAmount": {"uiAmountString": "100", "decimals": 6},
                }
            ],
            "postTokenBalances": [
                {
                    "owner": wallet,
                    "mint": mint,
                    "uiTokenAmount": {"uiAmountString": "150", "decimals": 6},
                }
            ],
        },
    }

    trades = parse_wallet_trades(transaction, wallet, "Signature111", 123)

    assert len(trades) == 1
    assert trades[0].side == "BUY"
    assert trades[0].mint == mint
    assert trades[0].token_delta == pytest.approx(50)
    assert trades[0].native_sol_delta == pytest.approx(-0.100005)


def market_quote(
    *,
    liquidity: float,
    market_cap: float,
    buys: int,
    sells: int,
    volume: float,
    change: float,
) -> MarketQuote:
    return MarketQuote(
        mint="MintScore111",
        symbol="SCORE",
        price_sol=0.000001,
        liquidity_usd=liquidity,
        market_cap_usd=market_cap,
        pair_address="Pair111",
        pair_created_at_ms=1,
        buys_m5=buys,
        sells_m5=sells,
        volume_m5_usd=volume,
        price_change_m5_pct=change,
    )


def test_intelligence_assigns_core_tier() -> None:
    result = CoinIntelligence().score(
        market_quote(
            liquidity=50_000,
            market_cap=100_000,
            buys=60,
            sells=20,
            volume=15_000,
            change=15,
        )
    )

    assert result.tier == "CORE"
    assert result.safety_score >= 35
    assert result.total_score >= 75


def test_intelligence_assigns_five_dollar_moonshot_tier() -> None:
    result = CoinIntelligence().score(
        market_quote(
            liquidity=10_000,
            market_cap=80_000,
            buys=25,
            sells=5,
            volume=10_000,
            change=80,
        )
    )

    assert result.tier == "MOONSHOT"
    assert result.total_score >= 60


def test_intelligence_hard_rejects_thin_liquidity() -> None:
    result = CoinIntelligence().score(
        market_quote(
            liquidity=1_000,
            market_cap=20_000,
            buys=100,
            sells=30,
            volume=50_000,
            change=100,
        )
    )

    assert result.tier == "REJECT"
    assert result.total_score == 0
