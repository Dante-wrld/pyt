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


@pytest.mark.parametrize('error, expected', [
    (TimeoutError('secret'), 'timed out'),
    (__import__('urllib.error', fromlist=['HTTPError']).HTTPError('https://secret', 429, 'secret', {}, None), 'HTTP 429'),
    (__import__('urllib.error', fromlist=['URLError']).URLError(__import__('ssl').SSLCertVerificationError('secret')), 'TLS'),
])
def test_rpc_errors_are_actionable_and_redacted(monkeypatch, error, expected):
    def fail(*args, **kwargs):
        assert kwargs['context'].check_hostname
        raise error
    monkeypatch.setattr('solana_launch_guard.robinhood_setup.urlopen', fail)
    with pytest.raises(ValueError, match=expected) as caught:
        ReadOnlyRpc('https://example.com/secret')('eth_chainId', [])
    assert 'secret' not in str(caught.value)


def test_contract_wallet_inspection_is_read_only_and_detects_7702():
    from solana_launch_guard.robinhood_setup import inspect_wallet
    account = Account.create()
    delegated = '0xef0100' + '34' * 20
    seen = []
    def rpc(method, params):
        seen.append((method, params))
        if method == 'eth_chainId': return hex(4663)
        if method == 'eth_blockNumber': return '0x10'
        if method == 'eth_getCode': return delegated
        if method == 'eth_call':
            assert params[0]['to'].lower() == account.address.lower()
            assert params[0]['data'].startswith('0x1626ba7e')
            from eth_abi import decode, encode
            from eth_account.messages import encode_defunct
            digest, signature = decode(['bytes32', 'bytes'], bytes.fromhex(params[0]['data'][10:]))
            assert len(signature) == 65
            assert Account.recover_message(encode_defunct(text='Launch Guard wallet compatibility check'), signature=signature) == account.address
            return '0x' + encode(['bytes4'], [bytes.fromhex('1626ba7e')]).hex()
        raise AssertionError(method)
    result = inspect_wallet(account.address, rpc, account.key.hex())
    assert result['eip7702_delegation_target'] == '0x' + '34' * 20
    assert result['eip1271_supported'] is True
    assert result['broadcast'] is False
    assert all(method != 'eth_sendRawTransaction' for method, _ in seen)


def test_inspection_preserves_delegation_when_signature_call_fails():
    from solana_launch_guard.robinhood_setup import inspect_wallet
    account = Account.create()
    def rpc(method, params):
        if method == 'eth_call': raise ValueError('provider rejected request')
        return {'eth_chainId': hex(4663), 'eth_blockNumber': '0x10', 'eth_getCode': '0xef0100' + '34' * 20}[method]
    result = inspect_wallet(account.address, rpc, account.key.hex())
    assert result['wallet_type'] == 'EIP7702_DELEGATED_EOA'
    assert result['eip1271_supported'] is None
    assert result['signature_probe_status'] == 'RPC_OR_CALL_FAILED'
    assert result['ready_for_direct_execution'] is False
