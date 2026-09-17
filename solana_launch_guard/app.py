from __future__ import annotations

import argparse
import asyncio
import json
import logging
import ssl
from collections.abc import Mapping
from typing import Any

import certifi
import websockets

from .config import Settings
from .core import (
    Launch,
    PaperBroker,
    RiskEngine,
    SQLiteStore,
    indicative_price,
)

LOGGER = logging.getLogger("solana_launch_guard")


class LaunchGuard:
    def __init__(self, settings: Settings, store: SQLiteStore) -> None:
        self.settings = settings
        self.store = store
        self.broker = PaperBroker(settings, store)
        self.risk = RiskEngine(settings)

    async def handle_launch(
        self, payload: Mapping[str, Any], websocket: Any | None = None
    ) -> None:
        try:
            launch = Launch.from_payload(payload)
        except ValueError as exc:
            LOGGER.warning("Ignoring malformed creation event: %s", exc)
            self.store.save_event("MALFORMED_CREATE", payload)
            return

        self.store.save_event("CREATE", payload, launch.mint)
        decision = self.risk.evaluate(launch, self.broker)
        self.store.save_decision(launch, decision)

        if not decision.accepted:
            LOGGER.info(
                "REJECT %-10s score=%d mint=%s reasons=%s",
                launch.symbol,
                decision.score,
                launch.mint,
                "; ".join(decision.reasons),
            )
            return

        position = self.broker.open(launch)
        LOGGER.info(
            "PAPER BUY %-10s %.6f SOL at %.12g SOL/token score=%d",
            position.symbol,
            position.cost_sol,
            position.entry_price_sol,
            decision.score,
        )

        if websocket is not None:
            await websocket.send(
                json.dumps(
                    {"method": "subscribeTokenTrade", "keys": [launch.mint]}
                )
            )

    async def handle_trade(
        self, payload: Mapping[str, Any], websocket: Any | None = None
    ) -> None:
        mint = str(payload.get("mint") or "").strip()
        if not mint or not self.broker.has_position(mint):
            return

        self.store.save_event("TRADE", payload, mint)
        price = indicative_price(payload)
        if price is None:
            LOGGER.debug("Trade for %s had no usable reserve price", mint)
            return

        position = self.broker.mark(mint, price)
        if position is None:
            return

        pnl_pct = (price / position.entry_price_sol - 1) * 100
        LOGGER.info(
            "MARK %-10s price=%.12g pnl=%+.2f%% status=%s",
            position.symbol,
            price,
            pnl_pct,
            position.status,
        )

        if position.status == "CLOSED":
            LOGGER.info(
                "PAPER SELL %-10s reason=%s pnl=%+.6f SOL (%+.2f%%)",
                position.symbol,
                position.exit_reason,
                position.pnl_sol or 0,
                position.pnl_pct or 0,
            )
            if websocket is not None:
                await websocket.send(
                    json.dumps(
                        {"method": "unsubscribeTokenTrade", "keys": [mint]}
                    )
                )

    async def handle_message(self, payload: Mapping[str, Any], websocket: Any) -> None:
        tx_type = str(payload.get("txType") or "").lower()
        mint = str(payload.get("mint") or "").strip()

        if tx_type == "create":
            await self.handle_launch(payload, websocket)
        elif mint and self.broker.has_position(mint):
            await self.handle_trade(payload, websocket)
        else:
            LOGGER.debug("Feed message ignored: %s", payload)

    async def run_forever(self) -> None:
        backoff_seconds = 1
        tls_context = ssl.create_default_context(cafile=certifi.where())
        while True:
            try:
                LOGGER.info("Connecting to the new-token feed")
                async with websockets.connect(
                    self.settings.websocket_uri,
                    ssl=tls_context,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                    max_size=2_000_000,
                ) as websocket:
                    await websocket.send(
                        json.dumps({"method": "subscribeNewToken"})
                    )
                    LOGGER.info("Subscribed to new token creations")
                    backoff_seconds = 1

                    async for raw_message in websocket:
                        try:
                            payload = json.loads(raw_message)
                        except (json.JSONDecodeError, TypeError):
                            LOGGER.warning("Ignoring non-JSON feed message")
                            continue
                        if not isinstance(payload, dict):
                            LOGGER.debug("Ignoring non-object feed message")
                            continue
                        await self.handle_message(payload, websocket)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning(
                    "Feed disconnected (%s). Reconnecting in %d seconds",
                    exc,
                    backoff_seconds,
                )
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 30)


async def run_demo(guard: LaunchGuard) -> None:
    """Exercise the full paper path without network access."""
    creation = {
        "signature": "demo-create-signature",
        "mint": "DemoMint111111111111111111111111111111111",
        "traderPublicKey": "DemoCreator11111111111111111111111111111",
        "txType": "create",
        "initialBuy": 1_000_000,
        "solAmount": 1,
        "bondingCurveKey": "DemoCurve111111111111111111111111111111",
        "vTokensInBondingCurve": 1_000_000_000,
        "vSolInBondingCurve": 30,
        "marketCapSol": 30,
        "name": "Demo Token",
        "symbol": "DEMO",
        "uri": "https://example.invalid/demo.json",
    }
    await guard.handle_launch(creation)

    if guard.broker.has_position(creation["mint"]):
        take_profit_trade = {
            "signature": "demo-trade-signature",
            "mint": creation["mint"],
            "txType": "buy",
            "vTokensInBondingCurve": 1_000_000_000,
            "vSolInBondingCurve": 40.5,
            "marketCapSol": 40.5,
        }
        await guard.handle_trade(take_profit_trade)

    LOGGER.info("Demo summary: %s", guard.store.summary())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor Pump.fun launches and simulate risk-gated trades."
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="run a deterministic offline paper-trade demonstration",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="print the database summary and exit",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    store = SQLiteStore(settings.database_path)
    guard = LaunchGuard(settings, store)
    try:
        if args.summary:
            print(json.dumps(store.summary(), indent=2))
        elif args.demo:
            asyncio.run(run_demo(guard))
        else:
            asyncio.run(guard.run_forever())
    except KeyboardInterrupt:
        LOGGER.info("Stopped by user")
    finally:
        store.close()


if __name__ == "__main__":
    main()
