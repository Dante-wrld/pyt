"""Local signer provisioning and read-only network checks; no execution API."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import warnings
from decimal import Decimal
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from eth_account import Account
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
            with urlopen(Request(self.url, data=data, headers={"Content-Type": "application/json"}), timeout=15) as response:
                result = json.load(response)
            if result.get("id") != 1 or "error" in result or "result" not in result:
                raise ValueError()
            return result["result"]
        except Exception:
            # Endpoint URLs and provider errors may contain credentials.
            raise ValueError("Robinhood RPC read failed; check endpoint/connectivity locally") from None


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
    parser.add_argument("--token", help="Optional public ERC-20 contract for --check-wallet")
    args = parser.parse_args()
    try:
        _load_dotenv()
        wallet = address(os.getenv("ROBINHOOD_WALLET_ADDRESS") or os.getenv("EVM_WALLET_ADDRESS") or "")
        if args.token and not args.check_wallet:
            raise ValueError("--token requires --check-wallet")
        if args.check_wallet:
            result = check_wallet(wallet, ReadOnlyRpc(os.getenv("ROBINHOOD_RPC_URL") or DEFAULT_RPC), args.token)
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
