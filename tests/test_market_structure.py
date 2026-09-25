import io
import json as jsonlib
from urllib.error import HTTPError

import pytest

from solana_launch_guard.market_structure import (
    Candle, MarketStructureScanner, PAIRS, assess_bullish_continuation_candle,
    assess_structure, parse_closed_candles,
)


def higher():
    bars = [Candle(i * 3600, 11, 14, 10, 11, 1000) for i in range(8)]
    bars.append(Candle(8 * 3600, 10.1, 11, 9.9, 10.5, 1500))
    return bars


def lower():
    start = 8 * 3600 + 20 * 60
    highs = [10.7, 10.7, 10.7, 11, 11, 10.9, 11, 11]
    lows = [10.4, 10.4, 10.4, 10.5, 10.5, 10.65, 10.69, 10.68]
    closes = [10.5, 10.5, 10.5, 10.85, 10.85, 10.8, 10.85, 10.9]
    return [
        Candle(start + i * 300, 10.5, highs[i], lows[i], closes[i], 100)
        for i in range(8)
    ]


def test_closed_candle_sweep_break_and_retest():
    now = 9 * 3600 + 60
    evidence = assess_structure(higher(), lower(), pair="H1/M5", now=now)
    assert evidence is not None
    assert evidence.invalidation == 9.9
    assert evidence.target == 14
    assert evidence.entry == 10.9
    assert "H1/M5" in evidence.description()


def test_structure_rejects_missing_sweep_break_stale_and_bad_pair():
    now = 9 * 3600 + 60
    assert assess_structure(higher()[:-1], lower(), pair="H1/M5", now=now) is None
    assert assess_structure(higher(), lower(), pair="H1/M5", now=now + 3 * 3600) is None
    assert assess_structure(higher(), lower()[:-1], pair="H1/M5", now=now) is None
    with pytest.raises(ValueError):
        assess_structure(higher(), lower(), pair="D/M1", now=now)


def test_parser_discards_open_bar_and_rejects_corrupted_candle():
    payload = {"data": {"attributes": {"ohlcv_list": [
        [0, 10, 11, 9, 10, 100],
        [300, 10, 11, 9, 10, 100],
    ]}}}
    assert len(parse_closed_candles(payload, period=300, now=500)) == 1
    payload["data"]["attributes"]["ohlcv_list"][0][2] = 8
    with pytest.raises(ValueError):
        parse_closed_candles(payload, period=300, now=500)


def test_required_timeframe_pairs_are_explicit():
    assert {key for key in PAIRS} == {"D/H1", "H4/M15", "H1/M5", "M15/M1"}


def test_bullish_continuation_confirms_a_long_body_with_a_small_lower_wick():
    # open 10 -> close 11.8 (body 1.8 of a 2.1 range); upper wick 0.2,
    # lower wick 0.1 - the CUMINU-chart shape: mostly body, and what little
    # wick there is sits above the close, not below it.
    candle = Candle(0, 10, 12, 9.9, 11.8, 500)
    evidence = assess_bullish_continuation_candle([candle], now=90)
    assert evidence is not None
    assert evidence["pattern"] == "bullish_continuation"
    assert evidence["upper_wick"] == pytest.approx(0.2)
    assert evidence["lower_wick"] == pytest.approx(0.1)


def test_bullish_continuation_rejects_a_lower_wick_bigger_than_the_upper_wick():
    candle = Candle(0, 10, 12, 9.5, 11.8, 500)
    assert assess_bullish_continuation_candle([candle], now=90) is None


def test_bullish_continuation_rejects_a_short_body_relative_to_the_range():
    candle = Candle(0, 10, 12, 9, 10.2, 500)
    assert assess_bullish_continuation_candle([candle], now=90) is None


def test_bullish_continuation_rejects_a_bearish_or_flat_candle():
    candle = Candle(0, 11.8, 12, 9.9, 10, 500)
    assert assess_bullish_continuation_candle([candle], now=90) is None


def test_bullish_continuation_rejects_a_stale_candle():
    candle = Candle(0, 10, 12, 9.9, 11.8, 500)
    assert assess_bullish_continuation_candle([candle], now=1000) is None


def test_bullish_continuation_rejects_corrupted_candle_values():
    # high below the close it's supposed to bound.
    candle = Candle(0, 10, 11, 9.9, 11.8, 500)
    assert assess_bullish_continuation_candle([candle], now=90) is None


def test_bullish_continuation_rejects_no_candles():
    assert assess_bullish_continuation_candle([], now=90) is None


FREE_OHLCV_URL = (
    "https://api.geckoterminal.com/api/v2/networks/solana/pools/POOL1/ohlcv/minute"
)
PAID_OHLCV_URL = (
    "https://pro-api.coingecko.com/api/v3/onchain/networks/solana/pools/POOL1/ohlcv/minute"
)


def _ohlcv_payload(mint):
    return jsonlib.dumps({
        "meta": {"base": {"address": mint}, "quote": {"address": "OTHER"}},
        "data": {"attributes": {"ohlcv_list": [
            [0, 1, 1.1, 0.9, 1, 100],
            [60, 1, 1.1, 0.9, 1, 100],
        ]}},
    }).encode()


def test_ohlcv_429_falls_back_to_the_paid_api_when_a_key_is_configured(monkeypatch):
    calls = []

    def urlopen(request, timeout=None, context=None):
        calls.append(request)
        if request.full_url.startswith(FREE_OHLCV_URL):
            raise HTTPError(request.full_url, 429, "Too Many Requests",
                            {}, io.BytesIO(b"rate limited"))
        if request.full_url.startswith(PAID_OHLCV_URL):
            return io.BytesIO(_ohlcv_payload("MINT1"))
        raise AssertionError(f"unexpected URL: {request.full_url}")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setenv("GECKOTERMINAL_API_KEY", "CG-test-key")
    scanner = MarketStructureScanner()
    candles = scanner._fetch("POOL1", "MINT1", ("minute", 1, 60))
    assert candles is not None and len(candles) == 2
    assert len(calls) == 2
    assert calls[0].full_url.startswith(FREE_OHLCV_URL)
    assert calls[1].full_url.startswith(PAID_OHLCV_URL)
    assert calls[1].get_header("X-cg-pro-api-key") == "CG-test-key"


def test_ohlcv_429_without_a_key_never_falls_back(monkeypatch):
    calls = []

    def urlopen(request, timeout=None, context=None):
        calls.append(request)
        raise HTTPError(request.full_url, 429, "Too Many Requests",
                        {}, io.BytesIO(b"rate limited"))

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.delenv("GECKOTERMINAL_API_KEY", raising=False)
    scanner = MarketStructureScanner()
    with pytest.raises(HTTPError):
        scanner._fetch("POOL1", "MINT1", ("minute", 1, 60))
    assert len(calls) == 1
