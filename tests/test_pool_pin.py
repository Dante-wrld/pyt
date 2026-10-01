"""Pool pinning (live quotes and tracker) and the historic pool-flip filter."""

import pytest
from solana_launch_guard.evaluation import Observation, drop_pool_flips
from solana_launch_guard.market import WSOL_MINT, DexScreenerOracle
from solana_launch_guard.outcome_tracker import parse_batch
from solana_launch_guard.pool_pin import PoolPinner

MINT = "8k4sBtEeK4pf26noKqApv8NBTnuSJcbdwpKYknk5PbAA"


def pick(pinner, *pools):
    """pools: (pair, liquidity)"""
    chosen = pinner.choose(
        "k", list(pools), pair_of=lambda p: p[0], liquidity_of=lambda p: p[1]
    )
    return None if chosen is None else chosen[0]


def test_first_poll_pins_the_deepest_pool():
    pinner = PoolPinner()
    assert pick(pinner, ("main", 500_000), ("side", 200_000)) == "main"
    assert pinner.pinned("k") == "main"


def test_missing_pinned_pool_skips_the_poll_instead_of_switching():
    pinner = PoolPinner()
    pick(pinner, ("main", 500_000), ("side", 200_000))
    assert pick(pinner, ("side", 210_000)) is None
    assert pick(pinner, ("main", 505_000), ("side", 210_000)) == "main"


def test_repins_after_a_real_migration():
    pinner = PoolPinner(repin_after=3)
    pick(pinner, ("curve", 80_000))
    assert pick(pinner, ("swap", 120_000)) is None
    assert pick(pinner, ("swap", 120_000)) is None
    assert pick(pinner, ("swap", 120_000)) == "swap"
    assert pinner.pinned("k") == "swap"


def test_a_side_pool_first_pin_moves_to_the_main_pool():
    pinner = PoolPinner(repin_after=2)
    pick(pinner, ("side", 200_000))  # main pool missing on the very first poll
    assert pick(pinner, ("main", 500_000), ("side", 200_000)) == "side"
    assert pick(pinner, ("main", 500_000), ("side", 200_000)) == "main"


def test_a_brief_deeper_rival_does_not_steal_the_pin():
    pinner = PoolPinner(repin_after=3)
    pick(pinner, ("main", 500_000))
    assert pick(pinner, ("main", 500_000), ("other", 2_000_000)) == "main"
    assert pick(pinner, ("main", 500_000)) == "main"  # strikes reset
    assert pinner.pinned("k") == "main"


def test_bad_settings_are_rejected():
    with pytest.raises(ValueError):
        PoolPinner(repin_after=0)
    with pytest.raises(ValueError):
        PoolPinner(weak_fraction=0)


def batch(*pools):
    return [
        {
            "chainId": "solana",
            "baseToken": {"address": MINT},
            "pairAddress": pair,
            "priceUsd": str(price),
            "liquidity": {"usd": liq},
        }
        for pair, price, liq in pools
    ]


def test_tracker_stays_on_the_pinned_pool_and_skips_when_it_is_missing():
    pools = PoolPinner()
    first = parse_batch(
        batch(("main", 0.0126, 512_000), ("side", 0.0032, 209_000)), [MINT], pools
    )
    assert first[MINT].price_usd == 0.0126
    flipped = parse_batch(batch(("side", 0.0032, 209_000)), [MINT], pools)
    assert MINT not in flipped  # retried next cycle, not recorded at $0.0032
    gone = parse_batch([], [MINT], pools)
    assert gone[MINT].price_usd is None  # no pairs at all is still "not found"


def test_tracker_without_a_pinner_keeps_the_old_behaviour():
    out = parse_batch(batch(("side", 0.0032, 209_000)), [MINT])
    assert out[MINT].price_usd == 0.0032


def market_pair(pair, price, liq):
    return {
        "chainId": "solana",
        "baseToken": {"address": MINT, "symbol": "X"},
        "quoteToken": {"address": WSOL_MINT},
        "pairAddress": pair,
        "priceNative": str(price),
        "priceUsd": str(price * 150),
        "liquidity": {"usd": liq},
        "txns": {"m5": {"buys": 3, "sells": 2}},
        "volume": {"m5": 100},
        "priceChange": {"m5": 1},
    }


def test_live_quotes_stay_on_one_pool():
    oracle = DexScreenerOracle()
    main = market_pair("main", 0.0001, 512_000)
    side = market_pair("side", 0.000025, 209_000)
    first = oracle._select_quote(MINT, [main, side])
    assert first is not None and first.pair_address == "main"
    assert oracle._select_quote(MINT, [side]) is None
    again = oracle._select_quote(MINT, [main, side])
    assert again is not None and again.price_sol == 0.0001


def obs(pairs):
    return [Observation(i * 60.0, p, liq, True) for i, (p, liq) in enumerate(pairs)]


def test_one_poll_pool_flips_are_dropped():
    path = obs(
        [
            (0.0126, 512e3),
            (0.0126, 513e3),
            (0.0032, 209e3),
            (0.0126, 512e3),
            (0.0126, 511e3),
            (0.0031, 210e3),
            (0.0032, 211e3),
            (0.0126, 514e3),
        ]
    )
    cleaned = drop_pool_flips(path)
    assert [o.price_usd for o in cleaned] == [0.0126] * 5


def test_real_crashes_and_rallies_are_kept():
    rug = obs(
        [
            (1.0, 90e3),
            (0.95, 88e3),
            (0.6, 59e3),
            (0.22, 37e3),
            (0.06, 20e3),
            (0.07, 21e3),
        ]
    )
    assert drop_pool_flips(rug) == rug
    rally = obs([(1.0, 50e3), (1.02, 51e3), (1.6, 51e3), (1.0, 50e3)])
    assert len(drop_pool_flips(rally)) == 4  # price spike without a pool change
    tail = obs([(1.0, 500e3), (1.0, 500e3), (0.25, 200e3)])
    assert drop_pool_flips(tail) == tail  # nothing after it to confirm a flip
