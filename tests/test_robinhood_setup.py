import pytest
from eth_account import Account
from solana_launch_guard.robinhood_setup import check_wallet, key_address, import_key, ReadOnlyRpc


def test_key_must_match_without_disclosing_secret():
    account = Account.create()
    assert key_address(account.key.hex(), account.address) == account.address
    with pytest.raises(ValueError, match="does not match") as error:
        key_address(account.key.hex(), "0x" + "12" * 20)
    assert account.key.hex() not in str(error.value)


def test_wrong_chain_stops_before_balance_read():
    calls = []
    def rpc(method, params):
        calls.append(method)
        return "0x1"
    with pytest.raises(ValueError, match="chain mismatch"):
        check_wallet("0x" + "12" * 20, rpc)
    assert calls == ["eth_chainId"]


def test_balances_use_same_block_and_never_claim_live_ready():
    calls = []
    def rpc(method, params):
        calls.append((method, params))
        return {"eth_chainId": hex(4663), "eth_blockNumber": "0x10", "eth_getBalance": hex(10**18), "eth_getCode": "0x"}[method]
    result = check_wallet("0x" + "12" * 20, rpc)
    assert result["native_balance_eth"] == "1"
    assert result["ready_for_live"] is False
    assert result["swap_simulated"] is False
    assert calls[2][1][-1] == calls[3][1][-1] == "0x10"


def test_rpc_cannot_broadcast():
    with pytest.raises(ValueError, match="read-only"):
        ReadOnlyRpc("https://example.com")("eth_sendRawTransaction", [])


def test_noninteractive_import_rejected(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(ValueError, match="interactive"):
        import_key("0x" + "12" * 20, None)


def test_token_and_gas_checks():
    def rpc(method, params):
        if method == "eth_call":
            return "0x" + format(6 if params[0]["data"] == "0x313ce567" else 1234567, "064x")
        return {"eth_chainId": hex(4663), "eth_blockNumber": "0x10", "eth_getBalance": "0x0", "eth_getCode": "0x1234"}[method]
    result = check_wallet("0x" + "12" * 20, rpc, "0x" + "34" * 20)
    assert result["token_balance_raw"] == "1234567"
    assert result["token_decimals"] == 6
    assert len(result["blockers"]) == 3
