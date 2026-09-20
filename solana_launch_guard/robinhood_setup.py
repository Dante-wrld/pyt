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


class ReadOnlyRpc:
    def __init__(self, url: str):
        if urlsplit(url).scheme != "https":
            raise ValueError("ROBINHOOD_RPC_URL must use HTTPS")
        self.url = url

    def __call__(self, method: str, params: list):
        if method not in {"eth_chainId", "eth_blockNumber", "eth_getBalance", "eth_getCode", "eth_call"}:
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
    calldata = "0x1626ba7e" + _bytes32(bytes(signed.message_hash)) + format(64, "064x") + format(len(signed.signature), "064x") + signed.signature.hex().ljust(64 * 2, "0")
    result = {
        "chain_id": CHAIN_ID, "wallet": wallet, "block": int(block, 16),
        "wallet_code_bytes": len(raw_code), "eip7702_delegation_target": None,
        "eip1271_supported": None, "broadcast": False, "live_execution": False,
        "ready_for_direct_execution": False,
        "next_step": "No execution path is enabled.",
    }
    if raw_code.startswith(bytes.fromhex("ef0100")) and len(raw_code) == 23:
        result["eip7702_delegation_target"] = "0x" + raw_code[3:].hex()
        result["next_step"] = "EIP-7702 delegation detected; sponsored-gas/account-abstraction compatibility still requires simulation."
    if raw_code:
        response = rpc("eth_call", [{"to": wallet, "data": calldata}, block])
        result["eip1271_supported"] = isinstance(response, str) and response.lower().startswith("0x1626ba7e")
        if result["eip1271_supported"]:
            result["next_step"] = "EIP-1271 signature accepted; account-abstraction route still requires simulation and a sponsor/bundler."
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
        "blockers": ["Robinhood swap execution adapter is not implemented"],
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
