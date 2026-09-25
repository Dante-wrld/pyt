"""GeckoTerminalClient: trending-pools parsing and MarketQuote conversion,
against a real response shape captured live 2026-09-23."""
import io
import json

import pytest

from solana_launch_guard.geckoterminal import (
    GeckoPool,
    GeckoTerminalClient,
    GeckoTerminalError,
    build_gecko_quotes,
)

TRENDING_POOLS_URL = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"

# A real trending_pools response row, trimmed, captured live 2026-09-23 -
# see geckoterminal.py's module docstring (this exact pool, PEPENOM, was
# created 2026-09-11 and still ranked as trending 12 days later).
TRENDING_POOLS_BODY = json.dumps({"data": [{
    "id": "solana_Bd4wKg3xEBKJ4Xrw8skXMmJ4W65gk3x8yd7AovuBJisZ",
    "type": "pool",
    "attributes": {
        "base_token_price_usd": "0.000397606528763949790462590757575688284840261868983497968608379275",
        "address": "Bd4wKg3xEBKJ4Xrw8skXMmJ4W65gk3x8yd7AovuBJisZ",
        "name": "PEPENOM / SOL",
        "pool_created_at": "2026-09-11T00:55:05Z",
        "fdv_usd": "235237.263975173",
        "market_cap_usd": None,
        "price_change_percentage": {"m5": "0.017", "h24": "4.611"},
        "transactions": {"m5": {"buys": 4, "sells": 1, "buyers": 4, "sellers": 1}},
        "volume_usd": {"m5": "73.8492128912", "h24": "59387.7343737141"},
        "reserve_in_usd": "130865.8622",
    },
    "relationships": {
        "base_token": {"data": {"id": "solana_EpEfnZxQyiBXppSKi8sncc8w4corn1UJbF9G91fQpump", "type": "token"}},
        "quote_token": {"data": {"id": "solana_So11111111111111111111111111111111111111112", "type": "token"}},
        "dex": {"data": {"id": "pumpswap", "type": "dex"}},
    },
}]}).encode()


def _routed_urlopen(routes: dict[str, bytes]):
    def urlopen(request, timeout=None, context=None):
        url = request.full_url if hasattr(request, "full_url") else request
        for prefix, body in routes.items():
            if url.startswith(prefix):
                return io.BytesIO(body)
        raise AssertionError(f"unexpected URL: {url}")
    return urlopen


def test_trending_pools_parses_a_real_shaped_response(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen({
        TRENDING_POOLS_URL: TRENDING_POOLS_BODY,
    }))
    client = GeckoTerminalClient()
    results = client._trending_pools(1)
    assert results == [GeckoPool(
        mint="EpEfnZxQyiBXppSKi8sncc8w4corn1UJbF9G91fQpump",
        symbol="PEPENOM",
        price_usd=0.000397606528763949790462590757575688284840261868983497968608379275,
        liquidity_usd=130865.8622,
        market_cap_usd=235237.263975173,  # falls back to fdv_usd (market_cap_usd was null)
        buys_m5=4, sells_m5=1, volume_m5_usd=73.8492128912,
        price_change_m5_pct=0.017,
        pool_created_at="2026-09-11T00:55:05Z",
    )]


def test_parse_pool_skips_a_row_missing_a_required_field():
    from solana_launch_guard.geckoterminal import _parse_pool
    broken = {"attributes": {}, "relationships": {}}
    assert _parse_pool(broken) is None


def test_build_gecko_quotes_converts_pool_created_at_to_pair_created_at_ms(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _routed_urlopen({
        TRENDING_POOLS_URL: TRENDING_POOLS_BODY,
    }))
    client = GeckoTerminalClient()
    pools = client._trending_pools(1)
    quotes = build_gecko_quotes(pools)
    quote = quotes["EpEfnZxQyiBXppSKi8sncc8w4corn1UJbF9G91fQpump"]
    assert quote.symbol == "PEPENOM"
    assert quote.chain == "solana"
    assert quote.liquidity_usd == 130865.8622
    # 2026-09-11T00:55:05Z in epoch milliseconds
    assert quote.pair_created_at_ms == 1789088105000


def test_http_error_raises_geckoterminal_error_with_body(monkeypatch):
    from urllib.error import HTTPError

    def urlopen(request, timeout=None, context=None):
        raise HTTPError(TRENDING_POOLS_URL, 429, "Too Many Requests",
                        {}, io.BytesIO(b'{"error":"rate limited"}'))

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = GeckoTerminalClient()
    with pytest.raises(GeckoTerminalError, match="rate limited"):
        client._trending_pools(1)


PAID_TRENDING_POOLS_URL = (
    "https://pro-api.coingecko.com/api/v3/onchain/networks/solana/trending_pools"
)


def test_429_falls_back_to_the_paid_api_when_a_key_is_configured(monkeypatch):
    from urllib.error import HTTPError

    calls = []

    def urlopen(request, timeout=None, context=None):
        calls.append(request)
        if request.full_url.startswith(TRENDING_POOLS_URL):
            raise HTTPError(request.full_url, 429, "Too Many Requests",
                            {}, io.BytesIO(b'{"error":"rate limited"}'))
        if request.full_url.startswith(PAID_TRENDING_POOLS_URL):
            return io.BytesIO(TRENDING_POOLS_BODY)
        raise AssertionError(f"unexpected URL: {request.full_url}")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = GeckoTerminalClient(api_key="CG-test-key")
    results = client._trending_pools(1)
    assert len(results) == 1
    assert len(calls) == 2
    assert calls[0].full_url.startswith(TRENDING_POOLS_URL)
    assert calls[0].get_header("X-cg-pro-api-key") is None
    assert calls[1].full_url.startswith(PAID_TRENDING_POOLS_URL)
    assert calls[1].get_header("X-cg-pro-api-key") == "CG-test-key"


def test_429_without_a_key_never_calls_the_paid_api(monkeypatch):
    from urllib.error import HTTPError

    calls = []

    def urlopen(request, timeout=None, context=None):
        calls.append(request)
        raise HTTPError(request.full_url, 429, "Too Many Requests",
                        {}, io.BytesIO(b'{"error":"rate limited"}'))

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = GeckoTerminalClient(api_key=None)
    with pytest.raises(GeckoTerminalError, match="rate limited"):
        client._trending_pools(1)
    assert len(calls) == 1


def test_a_non_429_error_never_falls_back_even_with_a_key(monkeypatch):
    from urllib.error import HTTPError

    calls = []

    def urlopen(request, timeout=None, context=None):
        calls.append(request)
        raise HTTPError(request.full_url, 500, "Server Error",
                        {}, io.BytesIO(b"boom"))

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = GeckoTerminalClient(api_key="CG-test-key")
    with pytest.raises(GeckoTerminalError, match="HTTP 500"):
        client._trending_pools(1)
    assert len(calls) == 1
