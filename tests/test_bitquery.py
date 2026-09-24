"""BitqueryClient: OAuth2 token refresh and LaunchLab discovery/trade
parsing, against real response shapes captured live 2026-09-23."""
import asyncio
import datetime
import io
import json

import pytest

from solana_launch_guard.bitquery import (
    LAUNCHLAB_STANDARD_SUPPLY,
    BitqueryAuthError,
    BitqueryClient,
    LaunchLabPool,
    LaunchLabPoolCreation,
    LaunchLabTrade,
    build_launchlab_quotes,
)

TOKEN_URL = "https://oauth2.bitquery.io/oauth2/token"
GRAPHQL_URL = "https://streaming.bitquery.io/graphql"

TOKEN_BODY = json.dumps(
    {"access_token": "test-token", "expires_in": 17999, "scope": "api", "token_type": "bearer"}
).encode()

# A real pool-creation response, trimmed to one row, captured live against
# Bitquery 2026-09-23 - see bitquery.py's module docstring. Nested under
# "creations", the alias the combined snapshot query gives this field (see
# _recent_launchlab_snapshot), not the raw "Instructions" field name.
POOL_CREATION_BODY = json.dumps({
    "data": {"Solana": {"creations": [{
        "Block": {"Time": "2026-09-23T07:56:47Z"},
        "Transaction": {
            "Signer": "3bEwPfUAuwu6eEtQmRTNDpxWCno9RYUXt9MJBy56XzD6",
            "Signature": "2hLdrNyox4ikQ3RF219jXcx5wgPt6HvnnKeYJQEuH7MuVwvnirbVBp6RSmcndpJbt5fyWK4DPcgDzT1yTVEDn1mf",
        },
        "Instruction": {
            "Accounts": [
                {"Address": "3bEwPfUAuwu6eEtQmRTNDpxWCno9RYUXt9MJBy56XzD6", "Token": {"Mint": "", "Owner": ""}},
                {"Address": "3WPyk6CgxRg4tgMwcrKStXfLSxZQc59koSvVUgbsiray",
                 "Token": {"Mint": "3WPyk6CgxRg4tgMwcrKStXfLSxZQc59koSvVUgbsiray",
                           "Owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}},
                {"Address": "So11111111111111111111111111111111111111112",
                 "Token": {"Mint": "So11111111111111111111111111111111111111112",
                           "Owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}},
            ],
            "Program": {"Arguments": [
                {"Name": "base_mint_param", "Value": {"json": json.dumps({
                    "decimals": 6, "name": "DIESEL FUEL COIN", "symbol": "DFCN",
                    "uri": "https://ipfs.io/ipfs/bafkreibhp5iwlvooqpbo7sgl73xan4kfbe57yaprcbqhzxhd7aynejzcpa",
                })}},
                {"Name": "curve_param", "Value": {"json": json.dumps({
                    "Enum": 0, "Constant": {"data": {
                        "supply": 1000000000000000, "total_base_sell": 793100000000000,
                        "total_quote_fund_raising": 85000000000, "migrate_type": 1,
                    }},
                })}},
                {"Name": "vesting_param", "Value": {"json": json.dumps(
                    {"total_locked_amount": 0, "cliff_period": 0, "unlock_period": 0}
                )}},
            ]},
        },
    }]}}
}).encode()

# A real trade response, trimmed to one row - nested under "trades", the
# alias the combined snapshot query gives this field.
TRADE_BODY = json.dumps({
    "data": {"Solana": {"trades": [{
        "Block": {"Time": "2026-09-23T08:27:34Z"},
        "Trade": {
            "Currency": {"MintAddress": "HTmQz7My6MehV7bjhJ6jde8nDND1yvsz68d24LP7YgUQ", "Symbol": "GP"},
            "PriceInUSD": 0.015908367667455122,
            "Side": {"Type": "sell", "AmountInUSD": "33.83653"},
        },
    }]}}
}).encode()

# A real pool response, trimmed to one row - note the quote currency is
# NOT SOL here (confirmed live: some LaunchLab pools, e.g. stonk.fun's
# stock-paired launches, are quoted against another token entirely).
# Nested under "pools", the alias the combined snapshot query gives this field.
POOL_BODY = json.dumps({
    "data": {"Solana": {"pools": [{
        "Block": {"Time": "2026-09-23T13:32:17Z"},
        "Pool": {
            "Base": {"PostAmountInUSD": "5240.602"},
            "Quote": {"PostAmountInUSD": "2471.9941"},
            "Market": {
                "BaseCurrency": {"MintAddress": "GePzjSdq6z1o8sgCYEGQo9kApBYdXUosTqXQuBJsap8p", "Symbol": "FORWARD"},
                "QuoteCurrency": {"MintAddress": "FWDtiB5fXHdVAewPqvHPL2dh4aBC1C6GacQbePoQXKjz", "Symbol": "FWDI"},
            },
        },
    }]}}
}).encode()


def _routed_urlopen(routes: dict[str, bytes]):
    def urlopen(request, timeout=None, context=None):
        url = request.full_url if hasattr(request, "full_url") else request
        for prefix, body in routes.items():
            if url.startswith(prefix):
                return io.BytesIO(body)
        raise AssertionError(f"unexpected URL: {url}")
    return urlopen


def test_rejects_missing_credentials():
    with pytest.raises(ValueError, match="client_id"):
        BitqueryClient("", "secret")
    with pytest.raises(ValueError, match="client_id"):
        BitqueryClient("id", "")


def test_access_token_is_fetched_once_and_cached(monkeypatch):
    calls = []

    def urlopen(request, timeout=None, context=None):
        calls.append(request.full_url)
        return io.BytesIO(TOKEN_BODY)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = BitqueryClient("id", "secret")
    first = client._access_token()
    second = client._access_token()
    assert first == second == "test-token"
    assert len(calls) == 1


def test_access_token_refreshes_after_expiry(monkeypatch):
    calls = []

    def urlopen(request, timeout=None, context=None):
        calls.append(request.full_url)
        return io.BytesIO(TOKEN_BODY)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = BitqueryClient("id", "secret")
    client._access_token()
    # Force expiry without waiting for the real ~5h token lifetime.
    client._token_expires_at = 0
    client._access_token()
    assert len(calls) == 2


def test_bad_token_response_raises_bitquery_auth_error(monkeypatch):
    def urlopen(request, timeout=None, context=None):
        return io.BytesIO(b'{"error": "invalid_client"}')

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = BitqueryClient("id", "secret")
    with pytest.raises(BitqueryAuthError):
        client._access_token()


def test_recent_launchlab_snapshot_parses_creations(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen({
        TOKEN_URL: TOKEN_BODY, GRAPHQL_URL: POOL_CREATION_BODY,
    }))
    client = BitqueryClient("id", "secret")
    creations, trades, pools = client._recent_launchlab_snapshot(20, 50, 50)
    assert creations == [LaunchLabPoolCreation(
        mint="3WPyk6CgxRg4tgMwcrKStXfLSxZQc59koSvVUgbsiray",
        name="DIESEL FUEL COIN", symbol="DFCN",
        creator="3bEwPfUAuwu6eEtQmRTNDpxWCno9RYUXt9MJBy56XzD6",
        signature="2hLdrNyox4ikQ3RF219jXcx5wgPt6HvnnKeYJQEuH7MuVwvnirbVBp6RSmcndpJbt5fyWK4DPcgDzT1yTVEDn1mf",
        block_time="2026-09-23T07:56:47Z", token_decimals=6,
        supply_raw=1000000000000000, total_base_sell_raw=793100000000000,
        total_quote_fund_raising_lamports=85000000000, migrate_type=1,
    )]
    assert trades == []
    assert pools == []


def test_recent_launchlab_snapshot_parses_trades(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen({
        TOKEN_URL: TOKEN_BODY, GRAPHQL_URL: TRADE_BODY,
    }))
    client = BitqueryClient("id", "secret")
    creations, trades, pools = client._recent_launchlab_snapshot(20, 50, 50)
    assert trades == [LaunchLabTrade(
        mint="HTmQz7My6MehV7bjhJ6jde8nDND1yvsz68d24LP7YgUQ", symbol="GP",
        side="sell", price_usd=0.015908367667455122, amount_usd=33.83653,
        block_time="2026-09-23T08:27:34Z",
    )]
    assert creations == []
    assert pools == []


def test_recent_launchlab_snapshot_parses_pools(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen({
        TOKEN_URL: TOKEN_BODY, GRAPHQL_URL: POOL_BODY,
    }))
    client = BitqueryClient("id", "secret")
    creations, trades, pools = client._recent_launchlab_snapshot(20, 50, 50)
    assert pools == [LaunchLabPool(
        mint="GePzjSdq6z1o8sgCYEGQo9kApBYdXUosTqXQuBJsap8p", symbol="FORWARD",
        liquidity_usd=5240.602 + 2471.9941,
        quote_mint="FWDtiB5fXHdVAewPqvHPL2dh4aBC1C6GacQbePoQXKjz",
        quote_symbol="FWDI", block_time="2026-09-23T13:32:17Z",
    )]
    assert creations == []
    assert trades == []


def test_recent_launchlab_snapshot_makes_one_graphql_call_not_three(monkeypatch):
    """The whole point of combining creations/trades/pools into one request:
    Bitquery bills a flat 5 points per call regardless of row count, so this
    must cost the same as any single one of the old three separate calls did
    alone - a two-thirds cut in LaunchLab's per-poll point spend."""
    combined = json.dumps({"data": {"Solana": {
        "creations": [], "trades": [], "pools": [],
    }}}).encode()
    graphql_calls = []

    def urlopen(request, timeout=None, context=None):
        url = request.full_url if hasattr(request, "full_url") else request
        if url == TOKEN_URL:
            return io.BytesIO(TOKEN_BODY)
        if url == GRAPHQL_URL:
            graphql_calls.append(url)
            return io.BytesIO(combined)
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = BitqueryClient("id", "secret")
    creations, trades, pools = client._recent_launchlab_snapshot(20, 50, 50)
    assert (creations, trades, pools) == ([], [], [])
    assert len(graphql_calls) == 1


def test_graphql_errors_raise_instead_of_silently_returning_empty(monkeypatch):
    error_body = json.dumps({"errors": [{"message": "bad query"}]}).encode()
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen({
        TOKEN_URL: TOKEN_BODY, GRAPHQL_URL: error_body,
    }))
    client = BitqueryClient("id", "secret")
    with pytest.raises(RuntimeError, match="bad query"):
        client._recent_launchlab_snapshot(20, 50, 50)


def test_recent_launchlab_snapshot_skips_a_creation_row_missing_the_mint_account(monkeypatch):
    """Accounts with no Token.Mint==Address match (the mint-identification
    heuristic) must not raise or fabricate an empty-string mint."""
    malformed = json.dumps({
        "data": {"Solana": {"creations": [{
            "Block": {"Time": "t"},
            "Transaction": {"Signer": "s", "Signature": "sig"},
            "Instruction": {
                "Accounts": [{"Address": "a", "Token": {"Mint": "", "Owner": ""}}],
                "Program": {"Arguments": [
                    {"Name": "base_mint_param", "Value": {"json": json.dumps(
                        {"decimals": 6, "name": "n", "symbol": "s"}
                    )}},
                    {"Name": "curve_param", "Value": {"json": json.dumps(
                        {"Constant": {"data": {"supply": 1, "total_base_sell": 1,
                                               "total_quote_fund_raising": 1}}}
                    )}},
                ]},
            },
        }]}}
    }).encode()
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen({
        TOKEN_URL: TOKEN_BODY, GRAPHQL_URL: malformed,
    }))
    client = BitqueryClient("id", "secret")
    assert client._recent_launchlab_snapshot(20, 50, 50) == ([], [], [])


def test_launchlab_activity_for_mints_short_circuits_on_an_empty_list(monkeypatch):
    """The trim-check's own caller never has a due mint until 600s of
    discovery have passed, so an empty list is the routine case, not an
    edge case - it must never spend a Bitquery call finding nothing to ask."""
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append(1))
    client = BitqueryClient("id", "secret")
    result = asyncio.run(client.launchlab_activity_for_mints([]))
    assert result == ([], [])
    assert calls == []


def test_launchlab_activity_for_mints_filters_by_the_given_mint_list(monkeypatch):
    """The whole point of this call over recent_launchlab_snapshot: fresh
    data for a SPECIFIC set of already-known mints, not another broad
    recent-N window that could crowd them out with unrelated launch
    volume in the gap since they were first seen."""
    combined = json.dumps({"data": {"Solana": {
        "trades": [{
            "Block": {"Time": "2026-09-23T08:27:34Z"},
            "Trade": {
                "Currency": {"MintAddress": "HTmQz7My6MehV7bjhJ6jde8nDND1yvsz68d24LP7YgUQ", "Symbol": "GP"},
                "PriceInUSD": 0.5,
                "Side": {"Type": "buy", "AmountInUSD": "10"},
            },
        }],
        "pools": [],
    }}}).encode()
    seen_queries = []

    def urlopen(request, timeout=None, context=None):
        url = request.full_url if hasattr(request, "full_url") else request
        if url == TOKEN_URL:
            return io.BytesIO(TOKEN_BODY)
        if url == GRAPHQL_URL:
            seen_queries.append(json.loads(request.data)["query"])
            return io.BytesIO(combined)
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = BitqueryClient("id", "secret")
    trades, pools = asyncio.run(client.launchlab_activity_for_mints(
        ["HTmQz7My6MehV7bjhJ6jde8nDND1yvsz68d24LP7YgUQ", "MukLDtJ8Cx9DxLbeyLRSWPSposTMWuwHANbuaudpump"],
    ))
    assert len(trades) == 1 and trades[0].price_usd == 0.5
    assert len(seen_queries) == 1
    query = seen_queries[0]
    assert "HTmQz7My6MehV7bjhJ6jde8nDND1yvsz68d24LP7YgUQ" in query
    assert "MukLDtJ8Cx9DxLbeyLRSWPSposTMWuwHANbuaudpump" in query
    # No creations lookup - these mints are already known from discovery.
    assert "Instructions" not in query


MINT = "A" * 44


def _trade(*, side, price_usd, amount_usd, block_time, mint=MINT, symbol="TEST"):
    return LaunchLabTrade(mint=mint, symbol=symbol, side=side, price_usd=price_usd,
                          amount_usd=amount_usd, block_time=block_time)


def _pool(*, liquidity_usd, mint=MINT, symbol="TEST"):
    return LaunchLabPool(mint=mint, symbol=symbol, liquidity_usd=liquidity_usd,
                         quote_mint="Q" * 44, quote_symbol="SOL", block_time="2026-09-23T00:00:00Z")


def test_build_launchlab_quotes_aggregates_a_5_minute_window():
    now = 1790000000.0
    def iso(offset_seconds):
        return datetime.datetime.fromtimestamp(
            now - offset_seconds, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    trades = [
        _trade(side="buy", price_usd=1.0, amount_usd=10, block_time=iso(600)),  # outside window
        _trade(side="buy", price_usd=1.1, amount_usd=20, block_time=iso(200)),
        _trade(side="sell", price_usd=1.2, amount_usd=15, block_time=iso(100)),
        _trade(side="buy", price_usd=1.3, amount_usd=25, block_time=iso(10)),
    ]
    pools = [_pool(liquidity_usd=60_000)]
    quotes = build_launchlab_quotes(trades=trades, pools=pools, now=now)
    assert set(quotes) == {MINT}
    quote = quotes[MINT]
    assert quote.price_usd == 1.3  # latest trade
    assert quote.liquidity_usd == 60_000
    assert quote.buys_m5 == 2  # the two within 5 minutes
    assert quote.sells_m5 == 1
    assert quote.volume_m5_usd == 20 + 15 + 25
    # price_change_m5_pct compares latest (1.3) to the oldest IN the window (1.1, at 200s)
    assert quote.price_change_m5_pct == pytest.approx((1.3 / 1.1 - 1) * 100)
    assert quote.market_cap_usd == 1.3 * LAUNCHLAB_STANDARD_SUPPLY
    assert quote.chain == "solana"


def test_build_launchlab_quotes_skips_a_mint_with_no_pool_liquidity():
    """Scoring requires real liquidity data (CoinIntelligence.score hard-
    rejects a None ratio) - a mint with trades but no matching pool must
    be skipped, never guessed at."""
    trades = [_trade(side="buy", price_usd=1.0, amount_usd=10, block_time="2026-09-23T00:00:00Z")]
    assert build_launchlab_quotes(trades=trades, pools=[], now=1790000000.0) == {}


def test_build_launchlab_quotes_prefers_the_mints_own_recorded_supply():
    creation = LaunchLabPoolCreation(
        mint=MINT, name="Test", symbol="TEST", creator="c", signature="s",
        block_time="2026-09-23T00:00:00Z", token_decimals=6,
        supply_raw=500_000_000_000_000, total_base_sell_raw=1, total_quote_fund_raising_lamports=1,
        migrate_type=1,
    )
    trades = [_trade(side="buy", price_usd=2.0, amount_usd=10, block_time="2026-09-23T00:00:00Z")]
    pools = [_pool(liquidity_usd=60_000)]
    quotes = build_launchlab_quotes(
        trades=trades, pools=pools, creations=(creation,), now=1790000000.0,
    )
    assert quotes[MINT].market_cap_usd == 2.0 * 500_000_000.0  # 500,000,000,000,000 / 1e6 decimals


def test_build_launchlab_quotes_falls_back_to_a_single_trade_outside_any_window():
    """A mint with only stale trade data (nothing within 5 minutes) still
    gets a best-effort quote from its single most recent trade, rather
    than an empty/undefined window - matches how a quiet position still
    reports its last known state elsewhere in the system."""
    trades = [_trade(side="sell", price_usd=3.0, amount_usd=5, block_time="2020-01-01T00:00:00Z")]
    pools = [_pool(liquidity_usd=10_000)]
    quotes = build_launchlab_quotes(trades=trades, pools=pools, now=1790000000.0)
    assert quotes[MINT].price_usd == 3.0
    assert quotes[MINT].sells_m5 == 1
    assert quotes[MINT].price_change_m5_pct == 0.0
