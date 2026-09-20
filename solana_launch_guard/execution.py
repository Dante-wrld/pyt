from __future__ import annotations

import asyncio
import base64
import getpass
import json
import math
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from typing import Any, ClassVar, Protocol

import certifi
import keyring
from keyring.errors import KeyringError
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.transaction import VersionedTransaction

JUPITER_SWAP_BASE_URL = "https://api.jup.ag/swap/v2"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
KEYRING_SERVICE = "solana-launch-guard"
KEYRING_ACCOUNT = "fomo-solana-private-key"


class QuoteGuardError(ValueError):
    """A Jupiter quote exceeded a configured price-impact or slippage cap."""


class AdditionalSignerError(ValueError):
    """A quoted transaction lacks a valid signature from another required signer."""


class JupiterRequestError(ConnectionError):
    """A sanitized Jupiter HTTP failure with any public execution evidence."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int,
        code: int | None = None,
        signature: str | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.signature = signature


class JupiterExecutionError(RuntimeError):
    """Jupiter returned a non-successful managed-execution result."""

    def __init__(
        self,
        message: str,
        *,
        code: int,
        signature: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.signature = signature


@dataclass(frozen=True, slots=True)
class SellIntent:
    mint: str
    symbol: str
    stage: int
    event_key: str
    amount_raw: int
    balance_raw: int
    decimals: int
    trigger_multiple: float
    current_multiple: float
    target_output_raw: int | None
    reason: str


@dataclass(frozen=True, slots=True)
class PreparedSell:
    intent: SellIntent
    transaction: str
    request_id: str
    input_amount_raw: int
    expected_output_raw: int
    minimum_output_raw: int
    price_impact_pct: float
    last_valid_block_height: str | None
    router: str | None = None
    mode: str | None = None
    slippage_bps: int | None = None
    fee_bps: int | None = None
    quoted_price_impact_pct: float | None = None
    quoted_slippage_bps: int | None = None
    reported_slippage_bps: int | None = None
    threshold_slippage_bps: int | None = None
    signature_fee_payer: str | None = None


@dataclass(frozen=True, slots=True)
class SellReceipt:
    intent: SellIntent
    signature: str
    input_amount_raw: int
    output_amount_raw: int


@dataclass(frozen=True, slots=True)
class PreflightReceipt:
    prepared: PreparedSell
    units_consumed: int | None
    log_count: int
    broadcast: bool = False
    adaptive_attempts: int = 1
    adaptive_rejections: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BuyIntent:
    mint: str
    symbol: str
    event_key: str
    amount_usdc_raw: int
    funding_source: str


@dataclass(frozen=True, slots=True)
class PreparedBuy:
    intent: BuyIntent
    transaction: str
    request_id: str
    input_amount_raw: int
    expected_output_raw: int
    minimum_output_raw: int
    price_impact_pct: float
    last_valid_block_height: str | None
    router: str | None = None
    mode: str | None = None
    slippage_bps: int | None = None
    fee_bps: int | None = None
    quoted_price_impact_pct: float | None = None
    quoted_slippage_bps: int | None = None
    reported_slippage_bps: int | None = None
    threshold_slippage_bps: int | None = None


@dataclass(frozen=True, slots=True)
class BuyReceipt:
    intent: BuyIntent
    signature: str
    input_amount_raw: int
    output_amount_raw: int


@dataclass(frozen=True, slots=True)
class BuyPreflightReceipt:
    prepared: PreparedBuy
    units_consumed: int | None
    log_count: int
    broadcast: bool = False


class OrderClient(Protocol):
    async def order(
        self,
        *,
        input_mint: str,
        output_mint: str = USDC_MINT,
        amount_raw: int,
        taker: str | None = None,
        exclude_routers: tuple[str, ...] = (),
    ) -> dict[str, Any]: ...

    async def execute(
        self,
        *,
        signed_transaction: str,
        request_id: str,
        last_valid_block_height: str | None,
    ) -> dict[str, Any]: ...


class TransactionSigner(Protocol):
    @property
    def public_key(self) -> str: ...

    def sign(self, transaction_b64: str) -> str: ...


class TransactionSimulator(Protocol):
    async def simulate_transaction(
        self, signed_transaction_b64: str
    ) -> dict[str, Any]: ...


class ProfitLadder:
    """Two-stage ladder for an owned Solana token.

    Stage 0 waits for a 2x price and sells enough tokens for the Jupiter
    minimum output to cover the original USD cost. Stage 1 waits for a 3x
    price (+200%) and sells half of the then-current token balance.
    """

    def __init__(
        self,
        *,
        principal_trigger_multiple: float = 2.0,
        half_profit_trigger_multiple: float = 3.0,
        second_stage_fraction: float = 0.5,
    ) -> None:
        self.principal_trigger_multiple = principal_trigger_multiple
        self.half_profit_trigger_multiple = half_profit_trigger_multiple
        self.second_stage_fraction = second_stage_fraction

    def plan(
        self,
        *,
        mint: str,
        symbol: str,
        stage: int,
        balance_raw: int,
        decimals: int,
        current_price_usd: float,
        entry_price_usd: float | None,
        original_cost_usd: float | None,
        cycle: int = 0,
    ) -> SellIntent | None:
        if (
            stage not in {0, 1}
            or balance_raw <= 0
            or decimals < 0
            or current_price_usd <= 0
            or entry_price_usd is None
            or entry_price_usd <= 0
            or original_cost_usd is None
            or original_cost_usd <= 0
        ):
            return None

        multiple = current_price_usd / entry_price_usd
        if stage == 0:
            if multiple < self.principal_trigger_multiple:
                return None
            units = original_cost_usd / current_price_usd
            amount_raw = math.ceil(units * (10**decimals))
            amount_raw = min(amount_raw, balance_raw)
            if amount_raw <= 0:
                return None
            target_output_raw = math.ceil(original_cost_usd * 1_000_000)
            reason = (
                f"price reached {multiple:.3f}x; recover the original "
                f"${original_cost_usd:.2f} into USDC"
            )
            trigger = self.principal_trigger_multiple
        else:
            if multiple < self.half_profit_trigger_multiple:
                return None
            amount_raw = math.floor(balance_raw * self.second_stage_fraction)
            if amount_raw <= 0:
                return None
            target_output_raw = math.ceil(
                amount_raw
                / (10**decimals)
                * entry_price_usd
                * self.half_profit_trigger_multiple
                * 1_000_000
            )
            reason = (
                f"price reached {multiple:.3f}x; sell "
                f"{self.second_stage_fraction:.0%} of the remaining tokens"
            )
            trigger = self.half_profit_trigger_multiple

        cycle_key = f":cycle:{cycle}" if cycle > 0 else ""
        return SellIntent(
            mint=mint,
            symbol=symbol,
            stage=stage,
            event_key=f"solana:{mint}:profit-ladder:{stage}{cycle_key}",
            amount_raw=amount_raw,
            balance_raw=balance_raw,
            decimals=decimals,
            trigger_multiple=trigger,
            current_multiple=multiple,
            target_output_raw=target_output_raw,
            reason=reason,
        )

    def preflight_plan(
        self,
        *,
        mint: str,
        symbol: str,
        stage: int,
        balance_raw: int,
        decimals: int,
        entry_price_usd: float | None,
        original_cost_usd: float | None,
    ) -> SellIntent:
        """Build the representative next-stage amount without a price trigger.

        Preflight uses the same amount that the next ladder stage would request
        at its configured trigger. It deliberately removes the minimum-USDC
        target because the token may not have reached that trigger yet.
        """
        if entry_price_usd is None or entry_price_usd <= 0:
            raise ValueError("preflight requires a positive USD entry price")
        trigger = (
            self.principal_trigger_multiple
            if stage == 0
            else self.half_profit_trigger_multiple
        )
        intent = self.plan(
            mint=mint,
            symbol=symbol,
            stage=stage,
            balance_raw=balance_raw,
            decimals=decimals,
            current_price_usd=entry_price_usd * trigger,
            entry_price_usd=entry_price_usd,
            original_cost_usd=original_cost_usd,
        )
        if intent is None:
            if stage not in {0, 1}:
                raise ValueError("profit ladder is already complete")
            raise ValueError("could not build a representative preflight amount")
        return replace(
            intent,
            event_key=f"solana:{mint}:preflight:{stage}",
            target_output_raw=None,
            reason=(
                f"preflight stage {stage + 1} transaction; simulation only, "
                "never broadcast"
            ),
        )


class PortfolioSignalExitPlanner:
    """Turn high-priority wallet guidance into bounded one-shot sell intents."""

    _STAGES: ClassVar[dict[str, int]] = {
        "TAKE PARTIAL": 10,
        "PROTECT PROFIT": 11,
        "EXIT WARNING": 12,
    }

    def __init__(
        self,
        *,
        take_partial_fraction: float = 0.5,
        protect_profit_fraction: float = 1.0,
        exit_warning_fraction: float = 1.0,
    ) -> None:
        self.fractions = {
            "TAKE PARTIAL": take_partial_fraction,
            "PROTECT PROFIT": protect_profit_fraction,
            "EXIT WARNING": exit_warning_fraction,
        }

    def plan(
        self,
        *,
        mint: str,
        symbol: str,
        decision: str,
        reason: str,
        balance_raw: int,
        decimals: int,
        cycle: int = 0,
    ) -> SellIntent | None:
        fraction = self.fractions.get(decision)
        if fraction is None or balance_raw <= 0 or decimals < 0:
            return None
        amount_raw = (
            balance_raw
            if fraction >= 1
            else math.floor(balance_raw * fraction)
        )
        if amount_raw <= 0:
            return None
        decision_key = decision.casefold().replace(" ", "-")
        cycle_key = f":cycle:{cycle}" if cycle > 0 else ""
        return SellIntent(
            mint=mint,
            symbol=symbol,
            stage=self._STAGES[decision],
            event_key=(
                f"solana:{mint}:portfolio-signal:{decision_key}{cycle_key}"
            ),
            amount_raw=amount_raw,
            balance_raw=balance_raw,
            decimals=decimals,
            trigger_multiple=0,
            current_multiple=0,
            target_output_raw=None,
            reason=f"{decision}: sell {fraction:.0%}; {reason}",
        )


def _slippage_bps_components(
    order: dict[str, Any], expected_output: int, minimum_output: int
) -> tuple[int, int, int]:
    reported = max(0, int(order.get("slippageBps") or 0))
    threshold = 0
    if expected_output <= 0 or minimum_output <= 0:
        return reported, reported, threshold
    threshold = math.ceil(
        max(0, expected_output - minimum_output) * 10_000 / expected_output
    )
    return max(reported, threshold), reported, threshold


def _evaluated_price_impact_pct(value: float, floor_percentages: bool) -> float:
    if not math.isfinite(value):
        raise ValueError("Jupiter returned a non-finite price impact")
    if not floor_percentages:
        return value
    return math.copysign(float(math.floor(abs(value))), value)


def _evaluated_slippage_bps(value: int, floor_percentages: bool) -> int:
    if not floor_percentages:
        return value
    return value // 100 * 100


def _last_valid_block_height(value: Any) -> str | None:
    """Normalize Jupiter's optional block height as a decimal string."""
    if value is None:
        return None
    text = str(value).strip()
    if not text.isdecimal():
        raise ValueError("Jupiter returned an invalid lastValidBlockHeight")
    return text


class KeyringSolanaSigner:
    def __init__(self, *, expected_public_key: str) -> None:
        try:
            secret = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except KeyringError as exc:
            raise RuntimeError("could not read the system keychain") from exc
        if not secret:
            raise ValueError(
                "Solana signing key is not in the system keychain; run "
                "launch-guard --store-fomo-solana-key"
            )
        self._keypair = parse_solana_keypair(secret)
        if str(self._keypair.pubkey()) != expected_public_key:
            raise ValueError("keychain signer does not match SOLANA_WALLET_ADDRESS")

    @property
    def public_key(self) -> str:
        return str(self._keypair.pubkey())

    def sign(self, transaction_b64: str) -> str:
        try:
            transaction = VersionedTransaction.from_bytes(
                base64.b64decode(transaction_b64, validate=True)
            )
        except (ValueError, TypeError) as exc:
            raise ValueError("Jupiter returned an invalid transaction") from exc

        required = transaction.message.header.num_required_signatures
        signer_keys = transaction.message.account_keys[:required]
        try:
            signer_index = signer_keys.index(self._keypair.pubkey())
        except ValueError as exc:
            raise ValueError(
                "configured wallet is not a required transaction signer"
            ) from exc

        signatures = list(transaction.signatures)
        if len(signatures) != required:
            raise ValueError("Jupiter transaction has an invalid signature count")
        signatures[signer_index] = self._keypair.sign_message(
            to_bytes_versioned(transaction.message)
        )
        signed = VersionedTransaction.populate(transaction.message, signatures)
        verified = signed.verify_with_results()
        if not verified[signer_index]:
            raise ValueError("wallet signature did not verify locally")
        if not all(verified):
            raise AdditionalSignerError(
                "Jupiter quote requires an invalid or missing additional signature"
            )
        return base64.b64encode(bytes(signed)).decode("ascii")


def parse_solana_keypair(secret: str) -> Keypair:
    value = secret.strip()
    try:
        if value.startswith("["):
            raw = json.loads(value)
            if not isinstance(raw, list) or len(raw) != 64:
                raise ValueError
            return Keypair.from_bytes(bytes(int(item) for item in raw))
        return Keypair.from_base58_string(value)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "exported Solana key must be a base58 secret or 64-byte JSON array"
        ) from exc


def store_fomo_solana_key(*, expected_public_key: str) -> str:
    secret = getpass.getpass(
        "Paste the exported Fomo Solana private key (input is hidden): "
    )
    keypair = parse_solana_keypair(secret)
    public_key = str(keypair.pubkey())
    if public_key != expected_public_key:
        raise ValueError(
            "exported key does not match SOLANA_WALLET_ADDRESS; nothing was stored"
        )
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, secret.strip())
    except KeyringError as exc:
        raise RuntimeError("could not write to the system keychain") from exc
    return public_key


class JupiterSwapClient:
    def __init__(self, *, api_key: str, timeout_seconds: float = 15) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._ssl = ssl.create_default_context(cafile=certifi.where())

    async def order(
        self,
        *,
        input_mint: str,
        output_mint: str = USDC_MINT,
        amount_raw: int,
        taker: str | None = None,
        exclude_routers: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        parameters = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
        }
        if taker is not None:
            parameters["taker"] = taker
        if exclude_routers:
            parameters["excludeRouters"] = ",".join(exclude_routers)
        query = urllib.parse.urlencode(parameters)
        return await asyncio.to_thread(
            self._request_json,
            f"{JUPITER_SWAP_BASE_URL}/order?{query}",
            None,
        )

    async def execute(
        self,
        *,
        signed_transaction: str,
        request_id: str,
        last_valid_block_height: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "signedTransaction": signed_transaction,
            "requestId": request_id,
        }
        if last_valid_block_height is not None:
            if not isinstance(last_valid_block_height, str) or not (
                last_valid_block_height.isdecimal()
            ):
                raise ValueError(
                    "Jupiter lastValidBlockHeight must be a decimal string"
                )
            payload["lastValidBlockHeight"] = last_valid_block_height
        return await asyncio.to_thread(
            self._request_json,
            f"{JUPITER_SWAP_BASE_URL}/execute",
            payload,
        )

    def _http_error_evidence(
        self, exc: urllib.error.HTTPError,
    ) -> tuple[str | None, int | None, str | None]:
        """Return allow-listed error detail, code, and signature only."""
        try:
            raw = exc.read(8_193)
        except (OSError, ValueError):
            return None, None, None
        if not raw:
            return None, None, None
        truncated = len(raw) > 8_192
        raw = raw[:8_192]
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            text = raw.decode("utf-8", errors="replace")
            text = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text)
            if self.api_key:
                text = text.replace(self.api_key, "[redacted-api-key]")
            text = re.sub(
                r"[A-Za-z0-9_+/=-]{64,}", "[redacted-long-value]", text
            )
            text = " ".join(text.split())[:300]
            if truncated:
                text = f"{text} [truncated]"
            return text or None, None, None
        if not isinstance(payload, dict):
            return None, None, None

        details: list[str] = []
        for key in ("errorCode", "errorMessage", "error", "message", "status"):
            value = payload.get(key)
            if isinstance(value, (str, int, float, bool)):
                safe_value = re.sub(
                    r"[\x00-\x1f\x7f-\x9f]", " ", str(value)
                )
                if self.api_key:
                    safe_value = safe_value.replace(
                        self.api_key, "[redacted-api-key]"
                    )
                safe_value = re.sub(
                    r"[A-Za-z0-9_+/=-]{64,}",
                    "[redacted-long-value]",
                    safe_value,
                )
                safe_value = " ".join(safe_value.split())
                details.append(f"{key}={safe_value[:300]}")
        raw_code = payload.get("code")
        try:
            code = int(raw_code) if raw_code is not None else None
        except (TypeError, ValueError):
            code = None
        signature_value = payload.get("signature")
        signature = None
        if isinstance(signature_value, str) and re.fullmatch(
            r"[1-9A-HJ-NP-Za-km-z]{32,128}", signature_value
        ):
            signature = signature_value
        detail = "; ".join(details)[:500] or None
        return detail, code, signature

    def _request_json(self, url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {
            "Accept": "application/json",
            "User-Agent": "solana-launch-guard/0.23.0",
            "x-api-key": self.api_key,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds, context=self._ssl
            ) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            detail, code, signature = self._http_error_evidence(exc)
            message = f"Jupiter request was rejected (HTTP {exc.code}"
            if code is not None:
                message += f", code {code}"
            message += ")"
            if detail:
                message += f": {detail}"
            raise JupiterRequestError(
                message,
                http_status=exc.code,
                code=code,
                signature=signature,
            ) from exc
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise ConnectionError(f"Jupiter request failed: {exc}") from exc
        if not isinstance(result, dict):
            raise ConnectionError("Jupiter returned an invalid response")
        return result


class SolanaAutoSeller:
    def __init__(
        self,
        *,
        client: OrderClient,
        signer: TransactionSigner,
        max_price_impact_pct: float = 5.0,
        max_slippage_bps: int = 500,
        floor_percentages: bool = False,
        max_principal_quote_attempts: int = 3,
    ) -> None:
        self.client = client
        self.signer = signer
        self.max_price_impact_pct = max_price_impact_pct
        self.max_slippage_bps = max_slippage_bps
        self.floor_percentages = floor_percentages
        self.max_principal_quote_attempts = max_principal_quote_attempts

    async def prepare(
        self,
        intent: SellIntent,
        *,
        exclude_routers: tuple[str, ...] = (),
    ) -> PreparedSell:
        amount_raw = intent.amount_raw
        order: dict[str, Any] | None = None
        for _ in range(self.max_principal_quote_attempts):
            order = await self.client.order(
                input_mint=intent.mint,
                amount_raw=amount_raw,
                taker=self.signer.public_key,
                exclude_routers=exclude_routers,
            )
            self._validate_order(order, intent.mint, amount_raw)
            minimum_output = int(
                order.get("otherAmountThreshold") or order.get("outAmount") or 0
            )
            if (
                intent.target_output_raw is None
                or minimum_output >= intent.target_output_raw
            ):
                break
            if intent.stage != 0:
                raise ValueError(
                    "Jupiter minimum output fell below the 3x trigger value"
                )
            if minimum_output <= 0:
                raise ValueError("Jupiter returned no usable USDC output")
            scaled = math.ceil(amount_raw * intent.target_output_raw / minimum_output)
            amount_raw = min(max(amount_raw + 1, scaled), intent.balance_raw)
        else:
            raise ValueError("principal recovery quote could not be satisfied")

        assert order is not None
        minimum_output = int(
            order.get("otherAmountThreshold") or order.get("outAmount") or 0
        )
        if minimum_output <= 0:
            raise ValueError("Jupiter returned no usable USDC output")
        if (
            intent.target_output_raw is not None
            and minimum_output < intent.target_output_raw
        ):
            raise ValueError(
                "current balance cannot recover principal after fees/slippage"
            )
        raw_price_impact = order.get("priceImpact")
        if raw_price_impact is None:
            price_impact = float(order.get("priceImpactPct") or 0) * 100
        else:
            price_impact = float(raw_price_impact)
        quoted_price_impact = price_impact
        price_impact = _evaluated_price_impact_pct(
            quoted_price_impact, self.floor_percentages
        )
        if abs(price_impact) > self.max_price_impact_pct:
            raise QuoteGuardError(
                f"Jupiter price impact {price_impact:.2f}% exceeds the "
                f"{self.max_price_impact_pct:.2f}% limit "
                f"(exact quote {quoted_price_impact:.6f}%)"
            )
        expected_output = int(order.get("outAmount") or 0)
        (
            quoted_slippage_bps,
            reported_slippage_bps,
            threshold_slippage_bps,
        ) = _slippage_bps_components(
            order, expected_output, minimum_output
        )
        slippage_bps = _evaluated_slippage_bps(
            quoted_slippage_bps, self.floor_percentages
        )
        if slippage_bps > self.max_slippage_bps:
            raise QuoteGuardError(
                f"Jupiter slippage {slippage_bps} bps exceeds the "
                f"{self.max_slippage_bps} bps limit "
                f"(exact effective {quoted_slippage_bps}; reported "
                f"{reported_slippage_bps}; output threshold "
                f"{threshold_slippage_bps})"
            )
        transaction = str(order.get("transaction") or "")
        request_id = str(order.get("requestId") or "")
        if not transaction or not request_id:
            code = order.get("errorCode")
            message = order.get("errorMessage") or "transaction unavailable"
            raise ValueError(f"Jupiter order {code}: {message}")
        last_valid = order.get("lastValidBlockHeight")
        return PreparedSell(
            intent=intent,
            transaction=transaction,
            request_id=request_id,
            input_amount_raw=amount_raw,
            expected_output_raw=expected_output,
            minimum_output_raw=minimum_output,
            price_impact_pct=price_impact,
            last_valid_block_height=_last_valid_block_height(last_valid),
            router=str(order.get("router")) if order.get("router") else None,
            mode=str(order.get("mode")) if order.get("mode") else None,
            slippage_bps=slippage_bps,
            fee_bps=(
                int(order["feeBps"])
                if order.get("feeBps") is not None
                else None
            ),
            quoted_price_impact_pct=quoted_price_impact,
            quoted_slippage_bps=quoted_slippage_bps,
            reported_slippage_bps=reported_slippage_bps,
            threshold_slippage_bps=threshold_slippage_bps,
            signature_fee_payer=(
                str(order["signatureFeePayer"])
                if order.get("signatureFeePayer") else None
            ),
        )

    async def execute(self, prepared: PreparedSell) -> SellReceipt:
        signed = self.signer.sign(prepared.transaction)
        result = await self.client.execute(
            signed_transaction=signed,
            request_id=prepared.request_id,
            last_valid_block_height=prepared.last_valid_block_height,
        )
        status = str(result.get("status") or "")
        code = int(result.get("code") or 0)
        signature = str(result.get("signature") or "")
        if status != "Success" or code != 0 or not signature:
            detail = result.get("error") or "transaction did not confirm"
            raise JupiterExecutionError(
                f"Jupiter execution uncertain/failed ({code}): {detail}",
                code=code,
                signature=signature or None,
            )
        return SellReceipt(
            intent=prepared.intent,
            signature=signature,
            input_amount_raw=int(
                result.get("totalInputAmount") or prepared.input_amount_raw
            ),
            output_amount_raw=int(
                result.get("totalOutputAmount") or result.get("outputAmountResult") or 0
            ),
        )

    async def preflight(
        self,
        intent: SellIntent,
        simulator: TransactionSimulator,
    ) -> PreflightReceipt:
        """Prepare, sign, and simulate a sell without submitting it."""
        # JupiterZ RFQ transactions require a market-maker signature that is
        # added only by /execute. Excluding that router keeps preflight fully
        # simulatable without ever calling the execution endpoint.
        excluded: tuple[str, ...] = ("jupiterz",)
        for _ in range(3):
            prepared = await self.prepare(intent, exclude_routers=excluded)
            if (
                prepared.signature_fee_payer is not None
                and prepared.signature_fee_payer != self.signer.public_key
            ):
                raise AdditionalSignerError(
                    "Jupiter returned a sponsored transaction requiring a "
                    "second signature, even with JupiterZ excluded; this "
                    "wallet cannot fully sign or signature-verify its sell "
                    "preflight. Check the wallet's SOL balance and fund its "
                    "network fees before retrying. No transaction broadcast."
                )
            try:
                signed = self.signer.sign(prepared.transaction)
                break
            except AdditionalSignerError:
                # Some router quotes require a signer other than this wallet.
                # Requote once per alternative router; every quote must pass
                # the existing amount, price impact, and slippage guards.
                router = (prepared.router or "").strip().lower()
                if not router or router in excluded or len(excluded) >= 3:
                    raise
                excluded += (router,)
        else:
            raise AdditionalSignerError("no fully signed alternative quote")
        simulation = await simulator.simulate_transaction(signed)
        logs = simulation.get("logs")
        units = simulation.get("unitsConsumed")
        return PreflightReceipt(
            prepared=prepared,
            units_consumed=int(units) if units is not None else None,
            log_count=len(logs) if isinstance(logs, list) else 0,
        )

    async def preflight_adaptive(
        self,
        intent: SellIntent,
        simulator: TransactionSimulator,
        *,
        minimum_amount_raw: int,
        max_attempts: int,
    ) -> PreflightReceipt:
        """Halve a portfolio-signal sell until its guarded quote can simulate."""
        if intent.target_output_raw is not None:
            return await self.preflight(intent, simulator)
        if max_attempts < 1:
            raise ValueError("adaptive sell max attempts must be at least one")
        minimum_amount_raw = min(
            intent.amount_raw, max(1, minimum_amount_raw)
        )
        amount_raw = intent.amount_raw
        rejections: list[str] = []
        for attempt in range(1, max_attempts + 1):
            attempt_intent = replace(intent, amount_raw=amount_raw)
            try:
                receipt = await self.preflight(attempt_intent, simulator)
            except QuoteGuardError as exc:
                rejections.append(f"{amount_raw}: {exc}")
                if amount_raw <= minimum_amount_raw:
                    break
                next_amount = max(minimum_amount_raw, amount_raw // 2)
                if next_amount >= amount_raw:
                    break
                amount_raw = next_amount
                continue
            return replace(
                receipt,
                adaptive_attempts=attempt,
                adaptive_rejections=tuple(rejections),
            )
        detail = rejections[-1] if rejections else "no quote was accepted"
        raise QuoteGuardError(
            "adaptive sell found no safe chunk after "
            f"{len(rejections)} quote attempt(s); last rejection: {detail}"
        )

    def _validate_order(
        self, order: dict[str, Any], input_mint: str, amount_raw: int
    ) -> None:
        if order.get("inputMint") not in {None, input_mint}:
            raise ValueError("Jupiter order input mint mismatch")
        if order.get("outputMint") not in {None, USDC_MINT}:
            raise ValueError("Jupiter order output mint mismatch")
        if int(order.get("inAmount") or amount_raw) != amount_raw:
            raise ValueError("Jupiter order input amount mismatch")


class SolanaAutoBuyer:
    """Prepare, simulate, and execute a guarded USDC token purchase."""

    def __init__(
        self,
        *,
        client: OrderClient,
        signer: TransactionSigner,
        max_price_impact_pct: float = 5.0,
        max_slippage_bps: int = 500,
        floor_percentages: bool = False,
    ) -> None:
        self.client = client
        self.signer = signer
        self.max_price_impact_pct = max_price_impact_pct
        self.max_slippage_bps = max_slippage_bps
        self.floor_percentages = floor_percentages

    async def prepare(
        self,
        intent: BuyIntent,
        *,
        exclude_routers: tuple[str, ...] = (),
    ) -> PreparedBuy:
        order = await self.client.order(
            input_mint=USDC_MINT,
            output_mint=intent.mint,
            amount_raw=intent.amount_usdc_raw,
            taker=self.signer.public_key,
            exclude_routers=exclude_routers,
        )
        self._validate_order(order, intent)
        expected_output = int(order.get("outAmount") or 0)
        minimum_output = int(
            order.get("otherAmountThreshold") or expected_output
        )
        if expected_output <= 0 or minimum_output <= 0:
            raise ValueError("Jupiter returned no usable token output")
        raw_price_impact = order.get("priceImpact")
        if raw_price_impact is None:
            price_impact = float(order.get("priceImpactPct") or 0) * 100
        else:
            price_impact = float(raw_price_impact)
        quoted_price_impact = price_impact
        price_impact = _evaluated_price_impact_pct(
            quoted_price_impact, self.floor_percentages
        )
        if abs(price_impact) > self.max_price_impact_pct:
            raise QuoteGuardError(
                f"Jupiter price impact {price_impact:.2f}% exceeds the "
                f"{self.max_price_impact_pct:.2f}% limit "
                f"(exact quote {quoted_price_impact:.6f}%)"
            )
        (
            quoted_slippage_bps,
            reported_slippage_bps,
            threshold_slippage_bps,
        ) = _slippage_bps_components(
            order, expected_output, minimum_output
        )
        slippage_bps = _evaluated_slippage_bps(
            quoted_slippage_bps, self.floor_percentages
        )
        if slippage_bps > self.max_slippage_bps:
            raise QuoteGuardError(
                f"Jupiter slippage {slippage_bps} bps exceeds the "
                f"{self.max_slippage_bps} bps limit "
                f"(exact effective {quoted_slippage_bps}; reported "
                f"{reported_slippage_bps}; output threshold "
                f"{threshold_slippage_bps})"
            )
        transaction = str(order.get("transaction") or "")
        request_id = str(order.get("requestId") or "")
        if not transaction or not request_id:
            code = order.get("errorCode")
            message = order.get("errorMessage") or "transaction unavailable"
            raise ValueError(f"Jupiter order {code}: {message}")
        last_valid = order.get("lastValidBlockHeight")
        return PreparedBuy(
            intent=intent,
            transaction=transaction,
            request_id=request_id,
            input_amount_raw=intent.amount_usdc_raw,
            expected_output_raw=expected_output,
            minimum_output_raw=minimum_output,
            price_impact_pct=price_impact,
            last_valid_block_height=_last_valid_block_height(last_valid),
            router=str(order.get("router")) if order.get("router") else None,
            mode=str(order.get("mode")) if order.get("mode") else None,
            slippage_bps=slippage_bps,
            fee_bps=(
                int(order["feeBps"])
                if order.get("feeBps") is not None
                else None
            ),
            quoted_price_impact_pct=quoted_price_impact,
            quoted_slippage_bps=quoted_slippage_bps,
            reported_slippage_bps=reported_slippage_bps,
            threshold_slippage_bps=threshold_slippage_bps,
        )

    async def execute(self, prepared: PreparedBuy) -> BuyReceipt:
        signed = self.signer.sign(prepared.transaction)
        result = await self.client.execute(
            signed_transaction=signed,
            request_id=prepared.request_id,
            last_valid_block_height=prepared.last_valid_block_height,
        )
        status = str(result.get("status") or "")
        code = int(result.get("code") or 0)
        signature = str(result.get("signature") or "")
        if status != "Success" or code != 0 or not signature:
            detail = result.get("error") or "transaction did not confirm"
            raise JupiterExecutionError(
                f"Jupiter execution uncertain/failed ({code}): {detail}",
                code=code,
                signature=signature or None,
            )
        return BuyReceipt(
            intent=prepared.intent,
            signature=signature,
            input_amount_raw=int(
                result.get("totalInputAmount") or prepared.input_amount_raw
            ),
            output_amount_raw=int(
                result.get("totalOutputAmount")
                or result.get("outputAmountResult")
                or 0
            ),
        )

    async def preflight(
        self,
        intent: BuyIntent,
        simulator: TransactionSimulator,
    ) -> BuyPreflightReceipt:
        prepared = await self.prepare(
            intent, exclude_routers=("jupiterz",)
        )
        signed = self.signer.sign(prepared.transaction)
        simulation = await simulator.simulate_transaction(signed)
        logs = simulation.get("logs")
        units = simulation.get("unitsConsumed")
        return BuyPreflightReceipt(
            prepared=prepared,
            units_consumed=int(units) if units is not None else None,
            log_count=len(logs) if isinstance(logs, list) else 0,
        )

    @staticmethod
    def _validate_order(order: dict[str, Any], intent: BuyIntent) -> None:
        if order.get("inputMint") not in {None, USDC_MINT}:
            raise ValueError("Jupiter order input mint mismatch")
        if order.get("outputMint") not in {None, intent.mint}:
            raise ValueError("Jupiter order output mint mismatch")
        input_amount = int(
            order.get("inAmount") or intent.amount_usdc_raw
        )
        if input_amount != intent.amount_usdc_raw:
            raise ValueError("Jupiter order input amount mismatch")
