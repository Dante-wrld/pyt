import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from solana_launch_guard import robinhood_agent_sell as a

TOKEN = '0x' + '12' * 20
OTHER = '0x' + '34' * 20


def view():
    return {'wallet': OTHER, 'token': TOKEN, 'requested_usd': '3',
            'token_balance_raw': '5000000', 'token_amount_to_sell_raw': '3000000',
            'market_price_usd': '1', 'estimated_proceeds_usd': '3',
            'approval_required': False, 'swap_simulated': True,
            'observed_at': time.time(), 'cost_basis': None}


def proposal(action='SELL', **overrides):
    return {'action': action, 'mint': TOKEN, 'requested_usd': 3,
            'confidence': .8, 'thesis': 'Risk review', **overrides}


def test_agent_cannot_change_sell_token_size_or_confidence():
    assert a.decide(proposal(), TOKEN, Decimal(3)) is not None
    assert a.decide(proposal('HOLD', requested_usd=0), TOKEN, Decimal(3)) is None
    for changes in ({'mint': OTHER}, {'requested_usd': 4}, {'confidence': .5},
                    {'action': 'BUY'}, {'action': 'REBUY'}):
        with pytest.raises(ValueError):
            a.decide(proposal(**changes), TOKEN, Decimal(3))


def test_invalid_token_and_missing_or_invalid_wallet_are_identified(monkeypatch):
    monkeypatch.delenv('ROBINHOOD_WALLET_ADDRESS', raising=False)
    monkeypatch.delenv('EVM_WALLET_ADDRESS', raising=False)
    with pytest.raises(ValueError, match='--token'):
        a.run('TOKEN_ADDRESS', 3)
    with pytest.raises(ValueError, match='Set ROBINHOOD_WALLET_ADDRESS'):
        a.preflight(TOKEN, Decimal(3))
    monkeypatch.setenv('ROBINHOOD_WALLET_ADDRESS', 'YOUR_PUBLIC_ADDRESS')
    with pytest.raises(ValueError, match='ROBINHOOD_WALLET_ADDRESS is not'):
        a.preflight(TOKEN, Decimal(3))
    monkeypatch.setenv('ROBINHOOD_WALLET_ADDRESS', '')
    monkeypatch.setenv('EVM_WALLET_ADDRESS', 'YOUR_PUBLIC_ADDRESS')
    with pytest.raises(ValueError, match='EVM_WALLET_ADDRESS is not'):
        a.preflight(TOKEN, Decimal(3))


def test_no_model_request_when_owned_sell_quote_or_gas_fails(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    def failed(*args):
        raise a.swap.TrialError('Pool SELL quote failed')
    monkeypatch.setattr(a, 'preflight', failed)
    monkeypatch.setattr(a.swap, 'main', lambda *args: pytest.fail('cannot broadcast'))
    with pytest.raises(ValueError, match='Pool SELL quote failed'):
        a.run(TOKEN, 3, model=SimpleNamespace(propose=lambda **kw: pytest.fail('no model')))


def test_hold_and_preview_never_enter_executor(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(a, 'preflight', lambda *args: view())
    monkeypatch.setattr(a.swap, 'main', lambda *args: pytest.fail('cannot broadcast'))
    model = SimpleNamespace(propose=lambda **kw: proposal('HOLD', requested_usd=0))
    assert a.run(TOKEN, 3, model=model)['broadcast'] is False
    model.propose = lambda **kw: proposal()
    result = a.run(TOKEN, 3, model=model)
    assert result['action'] == 'SELL' and result['broadcast'] is False
    assert not (tmp_path / a.SESSION).exists()


def test_live_session_one_fixed_sell_and_no_replay(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(a, 'preflight', lambda *args: view())
    monkeypatch.setattr(a.swap, 'enabled', lambda: None)
    calls = []
    monkeypatch.setattr(a.swap, 'main', lambda args: calls.append(args))
    model = SimpleNamespace(propose=lambda **kw: proposal())
    assert a.run(TOKEN, 3, execute=True, model=model) is None
    assert calls == [['--token', TOKEN, '--side', 'sell', '--usd', '3', '--execute']]
    with pytest.raises(ValueError, match='already been attempted'):
        a.run(TOKEN, 3, execute=True, model=model)
    assert len(calls) == 1
