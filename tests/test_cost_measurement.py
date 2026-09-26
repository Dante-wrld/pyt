import asyncio
import sqlite3

import pytest
from solana_launch_guard.core import SQLiteStore
from solana_launch_guard.cost_measurement import (
    USDC_MINT,
    measure_costs,
    measure_round_trip,
    realized_buy_slippage,
    recent_signal_mints,
    render_costs,
    summarize_costs,
)


class FakeJupiter:
    """Buys at 1 token per $1; the sell returns `keep` of the value."""

    def __init__(self, keep=0.99, fail_on=()):
        self.keep = keep
        self.fail_on = set(fail_on)
        self.calls = []

    async def order(self, *, input_mint, output_mint=USDC_MINT, amount_raw):
        self.calls.append((input_mint, output_mint, amount_raw))
        mint = output_mint if input_mint == USDC_MINT else input_mint
        if mint in self.fail_on:
            raise ConnectionError("rate limited")
        if input_mint == USDC_MINT:
            return {"outAmount": str(amount_raw * 1000), "priceImpact": "0.1"}
        return {"outAmount": str(int(amount_raw / 1000 * self.keep)),
                "priceImpact": "0.2"}


def test_round_trip_measures_the_full_buy_then_sell_gap():
    client = FakeJupiter(keep=0.98)
    trip = asyncio.run(measure_round_trip(client, "MINT", "MNT", 5.0))
    assert trip.cost_pct == pytest.approx(2.0)
    assert trip.usd_back == pytest.approx(4.9)
    # The sell quotes exactly the tokens the buy would return.
    assert client.calls[1] == ("MINT", USDC_MINT, 5_000_000_000)


def test_a_failed_quote_is_reported_not_guessed():
    trip = asyncio.run(measure_round_trip(FakeJupiter(fail_on={"BAD"}), "BAD", "B", 5))
    assert trip.cost_pct is None and "rate limited" in trip.error


def test_summary_suggests_a_per_side_cost_from_quotes_plus_slippage():
    client = FakeJupiter(keep=0.98)
    trips = asyncio.run(measure_costs(
        client, [("A", "A"), ("B", "B"), ("C", "C")], usd=5, pause_seconds=0,
    ))
    summary = summarize_costs(trips, slippage=[0.5, 0.3, -0.2])
    assert summary["round_trip_median_pct"] == pytest.approx(2.0)
    # half of 2% = 100 bps, plus 0.3% median realized slippage = 130 bps
    assert summary["suggested_slippage_bps_per_side"] == pytest.approx(130)
    text = render_costs(summary, usd=5)
    assert "--slippage-bps 130 --fee-bps 0" in text


def test_fills_better_than_the_quote_are_not_credited():
    trips = asyncio.run(measure_costs(FakeJupiter(keep=0.98), [("A", "A")], usd=5,
                                      pause_seconds=0))
    summary = summarize_costs(trips, slippage=[-1.0])
    assert summary["suggested_slippage_bps_per_side"] == pytest.approx(100)


def test_no_client_still_reports_realized_slippage():
    summary = summarize_costs(asyncio.run(measure_costs(None, [("A", "A")], usd=5)),
                              slippage=[0.4])
    assert summary["quoted"] == 0 and summary["realized_slippage_median_pct"] == 0.4
    assert "no quotes" in render_costs(summary, usd=5)


def test_reads_recent_mints_and_realized_slippage_read_only(tmp_path):
    path = tmp_path / "launch_guard.db"
    SQLiteStore(str(path)).close()
    db = sqlite3.connect(path)
    for mint in ["OLD", "NEW", "OLD", "EVM"]:
        db.execute(
            "INSERT INTO buy_signals(signaled_at, mint, symbol, chain, decision) "
            "VALUES ('2026-09-26T00:00:00+00:00', ?, ?, ?, 'BUY ZONE')",
            (mint, mint.lower(), "base" if mint == "EVM" else "solana"),
        )
    db.execute(
        "INSERT INTO auto_buy_executions(event_key, chain, token_address, symbol, "
        "funding_source, status, input_usdc_raw, expected_output_raw, "
        "actual_output_raw, output_decimals, created_at, updated_at) VALUES "
        "('k', 'solana', 'M', 'M', 'seed', 'CONFIRMED', 5000000, 1000, 990, 6, "
        "'t', 't')"
    )
    db.commit()
    db.close()
    before = path.read_bytes()
    assert recent_signal_mints(path, 5) == [("OLD", "old"), ("NEW", "new")]
    assert realized_buy_slippage(path) == [pytest.approx(1.0)]
    assert path.read_bytes() == before
