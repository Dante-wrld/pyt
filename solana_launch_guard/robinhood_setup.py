"""Local signer provisioning and read-only network checks; no execution API."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import ssl
import socket
import certifi
import warnings
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_abi import encode, decode
from .config import _load_dotenv

CHAIN_ID = 4663
SERVICE = "launch-guard-robinhood"
DEFAULT_RPC = "https://rpc.mainnet.chain.robinhood.com"


def address(value: str) -> str:
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", value) or int(value, 16) == 0:
        raise ValueError("A nonzero public EVM wallet/token address is required")
    return value.lower()


def key_address(secret: str, wallet: str) -> str:
    expected = address(wallet)
    try:
        derived = Account.from_key(secret.strip()).address
    except Exception:
        raise ValueError("Invalid private key; no key was saved") from None
    if derived.lower() != expected:
        raise ValueError("Private key does not match configured wallet; no key was saved")
    return derived


def keychain():
    # Explicit native backend: never fall back to plaintext/third-party backends.
    if sys.platform != "darwin":
        raise ValueError("Signer storage currently requires macOS Keychain")
    from keyring.backends.macOS import Keyring
    return Keyring()


def import_key(wallet: str, backend) -> str:
    wallet = address(wallet)
    if not sys.stdin.isatty():
        raise ValueError("Run import in an interactive terminal; piped keys are rejected")
    if backend.get_password(SERVICE, wallet) is not None:
        raise ValueError("A key is already stored for this wallet; run --verify-signer")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        secret = getpass.getpass("Robinhood private key (hidden, stored only in macOS Keychain): ")
    try:
        derived = key_address(secret, wallet)
        backend.set_password(SERVICE, wallet, secret.strip())
        return derived
    finally:
        secret = None  # Python cannot guarantee erasure of immutable strings.


def verify_key(wallet: str, backend) -> str:
    wallet = address(wallet)
    secret = backend.get_password(SERVICE, wallet)
    if not secret:
        raise ValueError("No Robinhood key stored; run --import-signer locally")
    try:
        return key_address(secret, wallet)
    finally:
        secret = None


def _revert_data(error) -> bytes | None:
    """Read bounded, strictly hexadecimal revert data without exposing RPC text."""
    value = error
    for _ in range(3):
        if isinstance(value, str):
            if len(value) > 4096:
                return None
            return bytes.fromhex(value[2:]) if re.fullmatch(r"0x[0-9a-fA-F]{8}(?:[0-9a-fA-F]{2})*", value) else None
        if not isinstance(value, dict):
            return None
        value = value.get("data", value.get("originalError"))
    return None


def _revert_selectors(error) -> tuple[str | None, str | None]:
    data = _revert_data(error)
    if data is None:
        return None, None
    outer = '0x' + data[:4].hex()
    # Uniswap v4 QuoterRevert.UnexpectedRevertBytes(bytes) wraps the actual
    # revert. Decode only the nested selector, never a dynamic error message.
    if outer != '0x6190b2b0' or len(data) < 72 or int.from_bytes(data[4:36], 'big') != 32:
        return outer, None
    length = int.from_bytes(data[36:68], 'big')
    if not 4 <= length <= len(data) - 68:
        return outer, None
    return outer, '0x' + data[68:72].hex()


class ReadOnlyRpc:
    METHODS = {"eth_chainId", "eth_blockNumber", "eth_getBalance", "eth_getCode", "eth_call"}
    def __init__(self, url: str):
        if urlsplit(url).scheme != "https":
            raise ValueError("ROBINHOOD_RPC_URL must use HTTPS")
        self.url = url

    def __call__(self, method: str, params: list):
        if method not in self.METHODS:
            raise ValueError("Only read-only RPC methods are permitted")
        data = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        try:
            with urlopen(Request(self.url, data=data, headers={"Content-Type": "application/json"}), timeout=15, context=ssl.create_default_context(cafile=certifi.where())) as response:
                result = json.load(response)
        except HTTPError as exc:
            raise ValueError(f"Robinhood RPC {method}: HTTP {exc.code}; check provider access or rate limits") from None
        except (TimeoutError, socket.timeout):
            raise ValueError(f"Robinhood RPC {method}: timed out after 15 seconds; try a working provider endpoint") from None
        except (URLError, ssl.SSLError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ssl.SSLError):
                category = "TLS certificate/handshake failure"
            elif isinstance(reason, socket.gaierror):
                category = "DNS lookup failed"
            elif isinstance(reason, TimeoutError):
                category = "connection timed out"
            else:
                category = "connection failed"
            raise ValueError(f"Robinhood RPC {method}: {category}") from None
        except Exception:
            raise ValueError(f"Robinhood RPC {method}: invalid or unreadable response") from None
        # Never print URLs, response bodies or server error text: they may echo keys.
        if not isinstance(result, dict) or result.get("id") != 1:
            raise ValueError(f"Robinhood RPC {method}: invalid response envelope")
        if "error" in result:
            error = result["error"]
            code = error.get("code") if isinstance(error, dict) else None
            suffix = f" (code {code})" if type(code) is int else ""
            selector, inner = _revert_selectors(error)
            if selector:
                suffix += f" (revert selector {selector})"
            if inner:
                suffix += f" (wrapped revert selector {inner})"
            raise ValueError(f"Robinhood RPC {method}: provider rejected request{suffix}")
        if "result" not in result:
            raise ValueError(f"Robinhood RPC {method}: missing result")
        return result["result"]


def _bytes32(value: bytes) -> str:
    return value.hex().rjust(64, "0")


def inspect_wallet(wallet: str, rpc, secret: str) -> dict:
    """Classify wallet code and test EIP-1271 through eth_call only."""
    wallet = address(wallet)
    if int(rpc("eth_chainId", []), 16) != CHAIN_ID:
        raise ValueError("RPC chain mismatch: expected Robinhood mainnet 4663")
    block = rpc("eth_blockNumber", [])
    code = rpc("eth_getCode", [wallet, block])
    if not isinstance(code, str) or not re.fullmatch(r"0x[0-9a-fA-F]*", code):
        raise ValueError("Invalid wallet code response")
    raw_code = bytes.fromhex(code[2:])
    account = Account.from_key(secret)
    if account.address.lower() != wallet:
        raise ValueError("Stored key does not match configured wallet")
    # A fixed harmless message binds a conventional EIP-191 signature to this address.
    signed = account.sign_message(encode_defunct(text="Launch Guard wallet compatibility check"))
    calldata = "0x1626ba7e" + encode(
        ["bytes32", "bytes"], [bytes(signed.message_hash), bytes(signed.signature)]
    ).hex()
    result = {
        "chain_id": CHAIN_ID, "wallet": wallet, "block": int(block, 16),
        "wallet_code_bytes": len(raw_code), "eip7702_delegation_target": None,
        "eip1271_supported": None, "signature_probe_status": "NOT_RUN",
        "wallet_type": "EOA" if not raw_code else "CONTRACT",
        "broadcast": False, "live_execution": False,
        "ready_for_direct_execution": False,
        "next_step": "No execution path is enabled.",
    }
    if raw_code.startswith(bytes.fromhex("ef0100")) and len(raw_code) == 23:
        result["eip7702_delegation_target"] = "0x" + raw_code[3:].hex()
        result["wallet_type"] = "EIP7702_DELEGATED_EOA"
        result["next_step"] = "Delegated EOAs may originate transactions; gas funding and swap simulation remain required."
    if raw_code:
        try:
            response = rpc("eth_call", [{"to": wallet, "data": calldata}, block])
        except ValueError:
            # Preserve code classification even when this optional probe fails.
            result["signature_probe_status"] = "RPC_OR_CALL_FAILED"
        else:
            try:
                if not isinstance(response, str) or not response.startswith("0x"):
                    raise ValueError()
                response_bytes = bytes.fromhex(response[2:])
                if len(response_bytes) != 32:
                    raise ValueError()
                magic = decode(["bytes4"], response_bytes)[0]
            except Exception:
                result["signature_probe_status"] = "INVALID_RETURN_DATA"
            else:
                result["eip1271_supported"] = magic == bytes.fromhex("1626ba7e")
                result["signature_probe_status"] = "ACCEPTED" if result["eip1271_supported"] else "NOT_ACCEPTED"
    result["signature_probe_scope"] = "Tests one EIP-191 signature only; failure does not prove EIP-1271 is unsupported."
    return result


def check_wallet(wallet: str, rpc, token: str | None = None) -> dict:
    wallet = address(wallet)
    if int(rpc("eth_chainId", []), 16) != CHAIN_ID:
        raise ValueError("RPC chain mismatch: expected Robinhood mainnet 4663")
    block = rpc("eth_blockNumber", [])
    wei = int(rpc("eth_getBalance", [wallet, block]), 16)
    code = rpc("eth_getCode", [wallet, block])
    result = {
        "chain_id": CHAIN_ID, "wallet": wallet, "block": int(block, 16),
        "native_balance_wei": str(wei), "native_balance_eth": str(Decimal(wei) / Decimal(10**18)),
        "wallet_has_code": code not in {"0x", "0x0", "0x00"},
        "live_execution": False, "broadcast": False, "swap_simulated": False,
        "ready_for_live": False,
        "blockers": ["Run robinhood_swap preflight to validate the route, approvals and gas"],
    }
    if wei == 0:
        result["blockers"].append("No native ETH balance for direct transaction gas")
    if result["wallet_has_code"]:
        result["blockers"].append("Contract/delegated wallet requires a separate signing compatibility review")
    if token:
        token = address(token)
        if rpc("eth_getCode", [token, block]) in {"0x", "0x0", "0x00"}:
            raise ValueError("Token address has no contract code on Robinhood Chain")
        def word(data):
            value = rpc("eth_call", [{"to": token, "data": data}, block])
            if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
                raise ValueError("Invalid ERC-20 response")
            return int(value, 16)
        raw = word("0x70a08231" + wallet[2:].zfill(64))
        decimals = word("0x313ce567")
        if decimals > 255:
            raise ValueError("Invalid ERC-20 decimals")
        result.update(token=token, token_balance_raw=str(raw), token_decimals=decimals)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--import-signer", action="store_true")
    group.add_argument("--verify-signer", action="store_true")
    group.add_argument("--check-wallet", action="store_true")
    group.add_argument("--inspect-wallet", action="store_true")
    parser.add_argument("--token", help="Optional public ERC-20 contract for --check-wallet")
    args = parser.parse_args()
    try:
        _load_dotenv()
        wallet = address(os.getenv("ROBINHOOD_WALLET_ADDRESS") or os.getenv("EVM_WALLET_ADDRESS") or "")
        if args.token and not args.check_wallet:
            raise ValueError("--token requires --check-wallet")
        rpc = ReadOnlyRpc(os.getenv("ROBINHOOD_RPC_URL") or DEFAULT_RPC)
        if args.check_wallet:
            result = check_wallet(wallet, rpc, args.token)
        elif args.inspect_wallet:
            backend = keychain()
            secret = backend.get_password(SERVICE, wallet)
            if not secret:
                raise ValueError("No Robinhood key stored; run --import-signer locally")
            try:
                result = inspect_wallet(wallet, rpc, secret)
                result["balance_check"] = check_wallet(wallet, rpc)
            finally:
                secret = None
        else:
            backend = keychain()
            derived = import_key(wallet, backend) if args.import_signer else verify_key(wallet, backend)
            result = {"wallet": derived, "signer_matches": True, "storage": "macOS Keychain", "broadcast": False, "live_execution": False}
        print(json.dumps(result, indent=2))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    except KeyboardInterrupt:
        raise SystemExit("Cancelled") from None
    except Exception:
        raise SystemExit("Local signer setup failed; no transaction was broadcast") from None


if __name__ == "__main__":
    main()
