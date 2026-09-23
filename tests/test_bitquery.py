"""BitqueryClient: OAuth2 token refresh and LaunchLab discovery/trade
parsing, against real response shapes captured live 2026-09-23."""
import io
import json

import pytest

from solana_launch_guard.bitquery import (
    BitqueryAuthError,
    BitqueryClient,
    LaunchLabPoolCreation,
    LaunchLabTrade,
)

TOKEN_URL = "https://oauth2.bitquery.io/oauth2/token"
GRAPHQL_URL = "https://streaming.bitquery.io/graphql"

TOKEN_BODY = json.dumps(
    {"access_token": "test-token", "expires_in": 17999, "scope": "api", "token_type": "bearer"}
).encode()

# A real pool-creation response, trimmed to one row, captured live against
# Bitquery 2026-09-23 - see bitquery.py's module docstring.
POOL_CREATION_BODY = json.dumps({
    "data": {"Solana": {"Instructions": [{
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

# A real trade response, trimmed to one row.
TRADE_BODY = json.dumps({
    "data": {"Solana": {"DEXTradeByTokens": [{
        "Block": {"Time": "2026-09-23T08:27:34Z"},
        "Trade": {
            "Currency": {"MintAddress": "HTmQz7My6MehV7bjhJ6jde8nDND1yvsz68d24LP7YgUQ", "Symbol": "GP"},
            "PriceInUSD": 0.015908367667455122,
            "Side": {"Type": "sell", "AmountInUSD": "33.83653"},
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


def test_recent_pool_creations_parses_a_real_shaped_response(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen({
        TOKEN_URL: TOKEN_BODY, GRAPHQL_URL: POOL_CREATION_BODY,
    }))
    client = BitqueryClient("id", "secret")
    results = client._recent_pool_creations(20)
    assert results == [LaunchLabPoolCreation(
        mint="3WPyk6CgxRg4tgMwcrKStXfLSxZQc59koSvVUgbsiray",
        name="DIESEL FUEL COIN", symbol="DFCN",
        creator="3bEwPfUAuwu6eEtQmRTNDpxWCno9RYUXt9MJBy56XzD6",
        signature="2hLdrNyox4ikQ3RF219jXcx5wgPt6HvnnKeYJQEuH7MuVwvnirbVBp6RSmcndpJbt5fyWK4DPcgDzT1yTVEDn1mf",
        block_time="2026-09-23T07:56:47Z", token_decimals=6,
        supply_raw=1000000000000000, total_base_sell_raw=793100000000000,
        total_quote_fund_raising_lamports=85000000000, migrate_type=1,
    )]


def test_recent_trades_parses_a_real_shaped_response(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen({
        TOKEN_URL: TOKEN_BODY, GRAPHQL_URL: TRADE_BODY,
    }))
    client = BitqueryClient("id", "secret")
    results = client._recent_trades(50)
    assert results == [LaunchLabTrade(
        mint="HTmQz7My6MehV7bjhJ6jde8nDND1yvsz68d24LP7YgUQ", symbol="GP",
        side="sell", price_usd=0.015908367667455122, amount_usd=33.83653,
        block_time="2026-09-23T08:27:34Z",
    )]


def test_graphql_errors_raise_instead_of_silently_returning_empty(monkeypatch):
    error_body = json.dumps({"errors": [{"message": "bad query"}]}).encode()
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen({
        TOKEN_URL: TOKEN_BODY, GRAPHQL_URL: error_body,
    }))
    client = BitqueryClient("id", "secret")
    with pytest.raises(RuntimeError, match="bad query"):
        client._recent_pool_creations(20)


def test_recent_pool_creations_skips_a_row_missing_the_mint_account(monkeypatch):
    """Accounts with no Token.Mint==Address match (the mint-identification
    heuristic) must not raise or fabricate an empty-string mint."""
    malformed = json.dumps({
        "data": {"Solana": {"Instructions": [{
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
    assert client._recent_pool_creations(20) == []
