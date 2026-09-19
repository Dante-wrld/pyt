import pytest

from solana_launch_guard.market_structure import (
    Candle, PAIRS, assess_structure, parse_closed_candles,
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
