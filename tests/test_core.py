from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from solana_launch_guard.app import _is_stock_token_symbol
from solana_launch_guard.config import Settings
from solana_launch_guard.core import (
    Launch,
    PaperBroker,
    RiskEngine,
    SQLiteStore,
    indicative_price,
)
from solana_launch_guard.intelligence import CoinIntelligence
from solana_launch_guard.market import DexScreenerOracle, MarketQuote
from solana_launch_guard.multichain import EvmRpc, HyperCoreWatcher
from solana_launch_guard.recommendations import (
    RecommendationBook,
    build_snapshot,
    format_dashboard,
    format_recommendations,
    read_snapshot,
    write_snapshot,
)
from solana_launch_guard.strategy import AdaptiveStrategy
from solana_launch_guard.wallet import parse_wallet_trades


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


def test_candidate_risk_ignores_full_paper_portfolio(tmp_path: Path) -> None:
    config = settings(
        tmp_path / "test.db",
        max_open_positions=1,
        max_total_exposure_sol=0.02,
    )
    store = SQLiteStore(config.database_path)
    broker = PaperBroker(config, store)
    broker.open(Launch.from_payload(launch_payload(mint="MintOne")))

    candidate = Launch.from_payload(launch_payload(mint="MintTwo"))
    assert RiskEngine(config).evaluate(candidate, broker).accepted is False
    assert RiskEngine(config).evaluate_candidate(candidate).accepted is True
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


def test_recommendations_rank_with_bounded_live_momentum() -> None:
    intelligence = CoinIntelligence()
    quote_a = market_quote(
        liquidity=50_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=15,
    )
    quote_b = MarketQuote(
        mint="MintScore222",
        symbol="FAST",
        price_sol=0.000001,
        liquidity_usd=50_000,
        market_cap_usd=100_000,
        pair_address="Pair222",
        pair_created_at_ms=1,
        buys_m5=60,
        sells_m5=20,
        volume_m5_usd=15_000,
        price_change_m5_pct=15,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60)
    book.add(quote_a, intelligence.score(quote_a), now=0)
    book.add(quote_b, intelligence.score(quote_b), now=0)
    book.update(
        MarketQuote(
            mint=quote_b.mint,
            symbol=quote_b.symbol,
            price_sol=0.0000012,
            liquidity_usd=quote_b.liquidity_usd,
            market_cap_usd=quote_b.market_cap_usd,
            pair_address=quote_b.pair_address,
            pair_created_at_ms=quote_b.pair_created_at_ms,
            buys_m5=quote_b.buys_m5,
            sells_m5=quote_b.sells_m5,
            volume_m5_usd=quote_b.volume_m5_usd,
            price_change_m5_pct=20,
        ),
        now=5,
    )

    ranked = book.ranked()

    assert ranked[0].mint == "MintScore222"
    assert ranked[0].rise_pct == pytest.approx(20)
    output = format_recommendations(ranked, color=True)
    assert "\033[38;5;220m" in output
    assert "mint=MintScore222" in output


def test_recommendations_expire_old_candidates() -> None:
    quote = market_quote(
        liquidity=50_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=15,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=10)
    book.add(quote, CoinIntelligence().score(quote), now=0)
    book.expire(now=11)
    assert book.ranked() == []


def test_recommendations_deduplicate_repeated_symbol() -> None:
    first = market_quote(
        liquidity=50_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=15,
    )
    second = MarketQuote(
        mint="DifferentMintSameSymbol",
        symbol=first.symbol.lower(),
        price_sol=first.price_sol,
        liquidity_usd=first.liquidity_usd,
        market_cap_usd=first.market_cap_usd,
        pair_address="PairDuplicate",
        pair_created_at_ms=first.pair_created_at_ms,
        buys_m5=first.buys_m5,
        sells_m5=first.sells_m5,
        volume_m5_usd=first.volume_m5_usd,
        price_change_m5_pct=first.price_change_m5_pct,
    )
    intelligence = CoinIntelligence()
    book = RecommendationBook(pool_size=10, ttl_seconds=60)
    book.add(first, intelligence.score(first), now=0)
    book.add(second, intelligence.score(second), now=1)

    ranked = book.ranked(limit=10)

    assert len(ranked) == 1
    assert ranked[0].symbol.casefold() == "score"


def test_snapshot_round_trip_and_colored_dashboard(tmp_path: Path) -> None:
    quote = market_quote(
        liquidity=50_000,
        market_cap=100_000,
        buys=60,
        sells=20,
        volume=15_000,
        change=15,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60)
    book.add(quote, CoinIntelligence().score(quote), now=0)
    snapshot = build_snapshot(book.ranked(), pending_count=4, poll_seconds=15)
    path = tmp_path / "recommendations.json"

    write_snapshot(path, snapshot)
    restored = read_snapshot(path)

    assert restored is not None
    assert restored["pending_count"] == 4
    assert len(restored["candidates"]) == 1
    output = format_dashboard(restored, color=True)
    assert "LAUNCH GUARD — PAPER BUY WATCHLIST" in output
    assert "Pending scans: 4" in output
    assert "mint=MintScore111" in output
    assert "\033[38;5;45m" in output


def test_robinhood_quote_uses_usd_and_exact_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    address = "0x5d102d1E69E77591D486aa4f663B3645151BBdf6"
    oracle = DexScreenerOracle()
    monkeypatch.setattr(
        oracle,
        "_request_token",
        lambda _address: [
            {
                "chainId": "robinhood",
                "pairAddress": "0xPair",
                "baseToken": {"address": address.lower(), "symbol": "VOLT"},
                "quoteToken": {"address": "0xQuote", "symbol": "WETH"},
                "priceNative": "0.0001",
                "priceUsd": "0.25",
                "liquidity": {"usd": 50000},
                "marketCap": 100000,
                "txns": {"m5": {"buys": 60, "sells": 20}},
                "volume": {"m5": 15000},
                "priceChange": {"m5": 15},
                "pairCreatedAt": 1,
            }
        ],
    )

    quote = oracle._fetch(address, "robinhood")

    assert quote is not None
    assert quote.chain == "robinhood"
    assert quote.price_sol == 0
    assert quote.price_usd == pytest.approx(0.25)
    assert quote.recommendation_key.endswith(address.lower())


def test_robinhood_profiles_and_stock_contracts_are_discovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meme = "0x1111111111111111111111111111111111111111"
    stock = "0x2222222222222222222222222222222222222222"
    oracle = DexScreenerOracle()

    def fake_request(url: str) -> object:
        if url.endswith("/rhj/assets"):
            return {
                "assets": [
                    {
                        "tokenSymbol": "NVDA",
                        "deployments": [
                            {"contractAddress": stock, "chainId": 4663}
                        ]
                    }
                ]
            }
        return [
            {"chainId": "robinhood", "tokenAddress": meme},
            {"chainId": "solana", "tokenAddress": "SolMint"},
        ]

    monkeypatch.setattr(oracle, "_request_json", fake_request)

    assert oracle._discover_token_profiles("robinhood") == (meme,)
    assert oracle._robinhood_stock_token_addresses() == frozenset(
        {stock.casefold()}
    )
    assert oracle._robinhood_stock_token_symbols() == frozenset({"nvda"})


def test_stock_symbols_and_wrapped_stock_symbols_are_excluded() -> None:
    symbols = frozenset({"nvda", "spy"})

    assert _is_stock_token_symbol("NVDA", symbols) is True
    assert _is_stock_token_symbol("wNVDAx", symbols) is True
    assert _is_stock_token_symbol("MEME", symbols) is False


def test_robinhood_recommendation_shows_contract_and_fomo_link() -> None:
    address = "0x3333333333333333333333333333333333333333"
    quote = MarketQuote(
        mint=address,
        symbol="RHCOIN",
        price_sol=0,
        price_usd=0.00025,
        chain="robinhood",
        liquidity_usd=50_000,
        market_cap_usd=100_000,
        pair_address="0xPair",
        pair_created_at_ms=1,
        buys_m5=60,
        sells_m5=20,
        volume_m5_usd=15_000,
        price_change_m5_pct=15,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60)
    book.add(quote, CoinIntelligence().score(quote), now=0)
    snapshot = build_snapshot(book.ranked(), pending_count=0, poll_seconds=15)

    output = format_dashboard(snapshot, color=False)

    assert "chain=RH" in output
    assert f"contract={address}" in output
    assert f"fomo=https://fomo.family/tokens/robinhood/{address}" in output
    assert "price=$0.00025" in output


def test_base_recommendation_uses_generic_evm_contract_display() -> None:
    address = "0x4444444444444444444444444444444444444444"
    quote = MarketQuote(
        mint=address,
        symbol="BASECOIN",
        price_sol=0,
        price_usd=0.05,
        chain="base",
        liquidity_usd=50_000,
        market_cap_usd=100_000,
        pair_address="0xPair",
        pair_created_at_ms=1,
        buys_m5=60,
        sells_m5=20,
        volume_m5_usd=15_000,
        price_change_m5_pct=15,
    )
    book = RecommendationBook(pool_size=10, ttl_seconds=60)
    book.add(quote, CoinIntelligence().score(quote), now=0)

    output = format_dashboard(
        build_snapshot(book.ranked(), pending_count=0, poll_seconds=15),
        color=False,
    )

    assert "chain=BASE" in output
    assert f"contract={address}" in output
    assert f"market=https://dexscreener.com/base/{address}" in output


def test_evm_transfer_parser_reads_incoming_erc20(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wallet = "0x1111111111111111111111111111111111111111"
    contract = "0x2222222222222222222222222222222222222222"
    rpc = EvmRpc("https://example.invalid")
    calls = 0

    def fake_request(method: str, _params: list[object]) -> object:
        nonlocal calls
        assert method == "eth_getLogs"
        calls += 1
        if calls == 1:
            return [
                {
                    "transactionHash": "0xabc",
                    "address": contract,
                    "logIndex": "0x2",
                    "blockNumber": "0x64",
                    "data": hex(1_500_000),
                }
            ]
        return []

    async def fake_metadata(_contract: str) -> tuple[str, int]:
        return "TOK", 6

    monkeypatch.setattr(rpc, "_request", fake_request)
    monkeypatch.setattr(rpc, "token_metadata", fake_metadata)

    transfers = asyncio.run(
        rpc.transfers(
            chain="base", wallet=wallet, from_block=1, to_block=100
        )
    )

    assert len(transfers) == 1
    assert transfers[0].direction == "IN"
    assert transfers[0].symbol == "TOK"
    assert transfers[0].token_amount == pytest.approx(1.5)


def test_hypercore_parses_spot_perps_and_fills() -> None:
    async def ignore(_item: object) -> None:
        return None

    watcher = HyperCoreWatcher(
        wallet="0x1111111111111111111111111111111111111111",
        fill_callback=ignore,
        state_callback=ignore,
    )
    state = watcher._parse_state(
        {"balances": [{"coin": "HYPE", "total": "2.5"}]},
        {
            "assetPositions": [
                {"position": {"coin": "BTC", "szi": "0.01"}}
            ]
        },
    )
    fills = watcher._parse_fills(
        [
            {
                "tid": 123,
                "coin": "HYPE",
                "side": "B",
                "sz": "1.5",
                "px": "20",
                "time": 1000,
            }
        ]
    )

    assert state.spot_balances == (("HYPE", 2.5),)
    assert state.perp_positions == (("BTC", 0.01),)
    assert fills[0].fill_id == "123"
    assert fills[0].price == pytest.approx(20)


def test_multichain_wallet_events_are_deduplicated(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "wallet.db")
    values = {
        "chain": "base",
        "wallet": "0x1111111111111111111111111111111111111111",
        "event_id": "0xabc:2",
        "block_number": 100,
        "token_address": "0x2222222222222222222222222222222222222222",
        "symbol": "TOK",
        "direction": "IN",
        "token_amount": 1.5,
        "price_usd": 0.25,
        "source": "EVM_TRANSFER",
    }

    assert store.save_wallet_event(**values) is True
    assert store.save_wallet_event(**values) is False
    info = store.multichain_wallet_info(values["wallet"])

    assert info["chains"][0]["chain"] == "base"
    assert info["chains"][0]["events"] == 1
    assert info["recent"][0]["symbol"] == "TOK"
    store.close()


def strategy_position() -> object:
    from solana_launch_guard.core import Position

    return Position(
        mint="MintScore111",
        symbol="SCORE",
        entry_price_sol=1.0,
        quantity=10,
        cost_sol=10,
        take_profit_price_sol=51,
        stop_loss_price_sol=0.6,
        opened_at="now",
        latest_price_sol=1.0,
    )


def test_adaptive_strategy_trails_after_peak_gain() -> None:
    strategy = AdaptiveStrategy(
        trailing_activation_pct=20,
        trailing_stop_pct=12,
    )
    position = strategy_position()
    first = market_quote(
        liquidity=50_000,
        market_cap=100_000,
        buys=30,
        sells=15,
        volume=10_000,
        change=20,
    )
    first = MarketQuote(
        **{**first.__dict__, "price_sol": 1.3}
    ) if hasattr(first, "__dict__") else MarketQuote(
        mint=first.mint,
        symbol=first.symbol,
        price_sol=1.3,
        liquidity_usd=first.liquidity_usd,
        market_cap_usd=first.market_cap_usd,
        pair_address=first.pair_address,
        pair_created_at_ms=first.pair_created_at_ms,
        buys_m5=first.buys_m5,
        sells_m5=first.sells_m5,
        volume_m5_usd=first.volume_m5_usd,
        price_change_m5_pct=first.price_change_m5_pct,
    )
    strategy.register_open(position, first, "MOONSHOT")
    pullback = MarketQuote(
        mint=first.mint,
        symbol=first.symbol,
        price_sol=1.1,
        liquidity_usd=50_000,
        market_cap_usd=100_000,
        pair_address="Pair111",
        pair_created_at_ms=1,
        buys_m5=20,
        sells_m5=15,
        volume_m5_usd=8_000,
        price_change_m5_pct=-2,
    )

    decision = strategy.evaluate_open(position, pullback)

    assert decision.action == "SELL"
    assert "TRAILING_STOP" in decision.reason


def test_adaptive_strategy_requires_recovery_for_reentry() -> None:
    strategy = AdaptiveStrategy(reentry_cooldown_seconds=0)
    position = strategy_position()
    entry_quote = MarketQuote(
        mint="MintScore111",
        symbol="SCORE",
        price_sol=1.0,
        liquidity_usd=50_000,
        market_cap_usd=100_000,
        pair_address="Pair111",
        pair_created_at_ms=1,
        buys_m5=20,
        sells_m5=10,
        volume_m5_usd=10_000,
        price_change_m5_pct=5,
    )
    strategy.register_open(position, entry_quote, "CORE")
    strategy.record_exit(position.mint, 0.9)
    recovery = MarketQuote(
        mint=entry_quote.mint,
        symbol=entry_quote.symbol,
        price_sol=0.95,
        liquidity_usd=48_000,
        market_cap_usd=95_000,
        pair_address="Pair111",
        pair_created_at_ms=1,
        buys_m5=30,
        sells_m5=10,
        volume_m5_usd=12_000,
        price_change_m5_pct=6,
    )

    decision = strategy.evaluate_reentry(recovery)

    assert decision.action == "REENTER"
    assert "RECOVERY" in decision.reason
