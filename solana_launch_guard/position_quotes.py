"""Shared price lookup for the paper books' held tokens.

wide-v1, wide-fresh-v1, momentum-take-v1 and swing-v1 each priced any held
token the board no longer covers with their own DexScreener request, every
cycle, from the same IP. Several books holding the same tokens meant several
identical requests, and with no retry a single 429 left that cycle's exits
unpriced (a position that cannot be priced cannot be sold, which matters for a
book whose whole job is a fast exit).

This process asks once instead: it reads the open positions of every book,
prices the union of those mints in batched requests with one long-lived client
(429s are retried with a short backoff, and pool pinning now survives between
polls), and writes launch_guard_position_quotes.json. swing_strategy.
dexscreener_quotes - the lookup every one of those books already calls - reads
that file first and only goes to DexScreener for a mint it does not cover, so
the books need no changes and still work if this process is not running.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
import urllib.error
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("solana_launch_guard.position_quotes")

DEFAULT_QUOTES_PATH = "launch_guard_position_quotes.json"
# A shared price older than this is ignored and the book prices the mint itself.
MAX_AGE_SECONDS = 20.0
RETRY_BACKOFF_SECONDS = (2.0, 5.0)
BOOKS = (
    ("WIDE_BOOK_PATH", "launch_guard_wide_capital.json"),
    ("WIDE_FRESH_BOOK_PATH", "launch_guard_wide_fresh_capital.json"),
    ("MOMENTUM_TAKE_BOOK_PATH", "launch_guard_momentum_take_capital.json"),
    ("SWING_BOOK_PATH", "launch_guard_swing_capital.json"),
)

QuoteBatchFetcher = Callable[[Sequence[str]], Awaitable[Mapping[str, Any]]]
Sleeper = Callable[[float], Awaitable[None]]


def quotes_path() -> str:
    return os.getenv("SHARED_QUOTES_PATH", DEFAULT_QUOTES_PATH)


def book_paths() -> list[str]:
    return [os.getenv(name, default) for name, default in BOOKS]


def held_mints(paths: Iterable[str | Path]) -> list[str]:
    """Every mint with an open position in any of these capital books."""
    held: set[str] = set()
    for path in paths:
        try:
            payload = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            continue
        agents = payload.get("agents") if isinstance(payload, dict) else None
        for account in (agents or {}).values():
            positions = account.get("positions") if isinstance(account, dict) else None
            held.update(m for m in (positions or {}) if isinstance(m, str))
    return sorted(held)


def read_shared_quotes(
    path: str | Path | None = None, *, now: float | None = None,
    max_age_seconds: float = MAX_AGE_SECONDS,
) -> dict[str, dict[str, Any]]:
    """Shared prices still fresh enough to use; {} if the file is missing."""
    try:
        payload = json.loads(Path(path or quotes_path()).read_text())
    except (OSError, ValueError):
        return {}
    at = time.time() if now is None else now
    out: dict[str, dict[str, Any]] = {}
    quotes = payload.get("quotes") if isinstance(payload, dict) else None
    for mint, quote in (quotes or {}).items():
        try:
            age = at - float(quote["quoted_at"])
            if 0 <= age <= max_age_seconds and float(quote["price"]) > 0:
                out[mint] = quote
        except (KeyError, TypeError, ValueError):
            continue
    return out


def write_shared_quotes(
    path: str | Path, quotes: Mapping[str, Mapping[str, Any]], *, now: float,
) -> None:
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps({"generated_at": now, "quotes": quotes}))
    tmp.replace(target)


def _is_rate_limit(exc: BaseException) -> bool:
    return isinstance(exc, urllib.error.HTTPError) and exc.code == 429


async def refresh_quotes(
    mints: Sequence[str], fetch: QuoteBatchFetcher, *,
    previous: Mapping[str, Mapping[str, Any]] | None = None,
    batch_size: int = 30, now: float | None = None,
    sleep: Sleeper = asyncio.sleep,
) -> dict[str, dict[str, Any]]:
    """Price `mints` in batches. A batch that still fails after the 429
    retries keeps its previous entries (their quoted_at ages out, so readers
    fall back to pricing those mints themselves); mints no longer held are
    dropped."""
    at = time.time() if now is None else now
    previous = previous or {}
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(mints), batch_size):
        batch = list(mints[start:start + batch_size])
        fetched: Mapping[str, Any] | None = None
        for attempt in range(len(RETRY_BACKOFF_SECONDS) + 1):
            try:
                fetched = await fetch(batch)
                break
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                if _is_rate_limit(exc) and attempt < len(RETRY_BACKOFF_SECONDS):
                    await sleep(RETRY_BACKOFF_SECONDS[attempt])
                    continue
                LOGGER.warning("position quotes: %d mints not priced: %s",
                               len(batch), exc)
                break
        if fetched is None:
            out.update({m: dict(previous[m]) for m in batch if m in previous})
            continue
        for mint, quote in fetched.items():
            price = getattr(quote, "price_usd", None)
            if price and price > 0:
                out[mint] = {
                    "price": price, "price_currency": "USD",
                    "liquidity_usd": getattr(quote, "liquidity_usd", None),
                    "quoted_at": at,
                }
    return out


def main(argv: Sequence[str] | None = None) -> None:
    from .eval_cli import _load_dotenv
    from .outcome_tracker import BATCH_SIZE, DexScreenerBatchClient

    _load_dotenv()
    parser = argparse.ArgumentParser(
        prog="launch-guard-position-quotes",
        description="price every paper book's held tokens once, for all of them")
    parser.add_argument("--interval", type=float, default=float(
        os.getenv("SHARED_QUOTES_INTERVAL_SECONDS", "10")))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    client = DexScreenerBatchClient()  # one client: pool pinning persists
    path = quotes_path()
    previous: dict[str, dict[str, Any]] = {}
    LOGGER.warning("position quotes: %s every %gs for %s", path,
                   max(args.interval, 5.0), ", ".join(p for p in book_paths()))
    while True:
        try:
            mints = held_mints(book_paths())
            previous = asyncio.run(refresh_quotes(
                mints, client.quotes, previous=previous, batch_size=BATCH_SIZE))
            write_shared_quotes(path, previous, now=time.time())
        except (OSError, ValueError, RuntimeError) as exc:
            LOGGER.warning("position quotes cycle skipped: %s", exc)
        if args.once:
            return
        time.sleep(max(args.interval, 5.0))


if __name__ == "__main__":
    main()
