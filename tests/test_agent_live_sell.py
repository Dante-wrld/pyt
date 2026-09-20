import asyncio
import json
import time
import sys
from types import SimpleNamespace
import pytest
from solana_launch_guard.agent_live_sell import eligible, validate_choice, run

MINT = 'A' * 32

def row():
    return dict(chain='solana', token_address=MINT, decision='EXIT WARNING', current_value_usd=4, current_price=1, liquidity_usd=10000)

def test_eligibility_fails_closed():
    now = time.time()
    assert eligible({'generated_at': now, 'signals': [row()]}, now)
    for timestamp in (now-16, now+1, float('nan')):
        assert not eligible({'generated_at': timestamp, 'signals': [row()]}, now)
    for field, value in [('chain', 'robinhood'), ('current_value_usd', 1.99), ('current_value_usd', 5.01), ('decision', 'HOLD'), ('current_price', float('nan'))]:
        r=row();r[field]=value
        assert not eligible({'generated_at': now, 'signals': [r]}, now)

def test_choice_cannot_change_mint_or_exceed_cap():
    raw=dict(action='SELL', mint=MINT, requested_usd=4, confidence=.8, thesis='Exit')
    assert validate_choice(raw, [row()]).mint == MINT
    for changes in ({'mint':'B'*32}, {'requested_usd':5.5}, {'confidence':.5}, {'requested_usd':2}):
        with pytest.raises(ValueError): validate_choice({**raw, **changes}, [row()])
    assert validate_choice({**raw, 'action':'HOLD', 'requested_usd':0}, [row()]) is None

def test_session_one_sale_and_verification_then_no_repeat(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k,v in dict(AGENT_LIVE_TEST_ENABLED='true', AGENT_LIVE_KILL_SWITCH='false', AUTO_BUY_LIVE='false', AUTO_SELL_LIVE='false').items(): monkeypatch.setenv(k,v)
    (tmp_path/'launch_guard_portfolio.json').write_text(json.dumps({'generated_at':time.time(), 'signals':[row()]}))
    calls=[]
    class Model:
        client = SimpleNamespace(with_options=lambda **kw: None)
        def propose(self, **kwargs):
            calls.append('model')
            return dict(action='SELL', mint=MINT, requested_usd=4, confidence=.8, thesis='Exit')
    class Store:
        def __init__(self,*args): pass
        def close(self): calls.append('close')
    async def execute(settings, store, mint, confirmation, before_broadcast):
        before_broadcast();calls.append('execute');return {'signature':'test', 'broadcast':True}
    async def verify(*args):
        calls.append('verify');return {'transaction_confirmed':True}
    monkeypatch.setattr('solana_launch_guard.agent_live_sell.replace', lambda settings, **kw: settings)
    monkeypatch.setitem(sys.modules,'solana_launch_guard.config',SimpleNamespace(Settings=SimpleNamespace(from_env=lambda:SimpleNamespace(solana_wallet_address='test',jupiter_api_key='test',database_path='test',auto_sell_max_price_impact_pct=3,auto_sell_max_slippage_bps=300,portfolio_min_sell_value_usd=2))))
    monkeypatch.setitem(sys.modules,'solana_launch_guard.core',SimpleNamespace(SQLiteStore=Store))
    monkeypatch.setitem(sys.modules,'solana_launch_guard.execution',SimpleNamespace(KeyringSolanaSigner=lambda **kw:SimpleNamespace(public_key='test')))
    monkeypatch.setitem(sys.modules,'solana_launch_guard.openai_agents',SimpleNamespace(OpenAIProposalModel=Model))
    monkeypatch.setitem(sys.modules,'solana_launch_guard.app',SimpleNamespace(execute_owned_sell_once=execute,verify_owned_sell=verify))
    result=asyncio.run(run())
    assert result['status']=='VERIFIED'
    assert calls==['model','execute','verify','close']
    with pytest.raises(ValueError): asyncio.run(run())
    assert calls.count('execute')==1
