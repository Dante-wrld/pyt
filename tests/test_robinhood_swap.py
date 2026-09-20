import json
import time
from decimal import Decimal

import pytest
from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak
from solana_launch_guard import robinhood_swap as s

TOKEN='0x'+'12'*20
WALLET='0x'+'34'*20
KEY=(s.ZERO,TOKEN,3000,60,s.ZERO)
POOL='0x'+keccak(encode([s.POOL_TYPE],[KEY])).hex()


def test_release_212_calldata_has_per_hop_price_and_exact_settlement():
    data=s.swap_data(KEY,True,1000,900,999)
    assert data[:10] == s.calldata('execute(bytes,bytes[],uint256)')[:10]
    commands,inputs,deadline=decode(['bytes','bytes[]','uint256'],bytes.fromhex(data[10:]))
    assert commands == b'\x10\x04' and deadline == 999
    assert decode(['address','address','uint256'],inputs[1]) == (s.ZERO,'0x'+'00'*19+'01',0)
    actions,params=decode(['bytes','bytes[]'],inputs[0])
    assert actions == bytes.fromhex('060c0f')
    swap=decode([f'({s.POOL_TYPE},bool,uint128,uint128,uint256,bytes)'],params[0])[0]
    assert swap == (KEY,True,1000,900,9*10**35,b'')
    assert decode(['address','uint256'],params[1]) == (s.ZERO,1000)
    assert decode(['address','uint256'],params[2]) == (TOKEN,900)


def test_pool_key_checks_currency_hook_and_hash():
    s.validate_pool(KEY,TOKEN,POOL)
    with pytest.raises(s.TrialError,match='hash'):
        s.validate_pool(KEY,TOKEN,'0x'+'00'*32)
    hooked=(*KEY[:4],WALLET)
    with pytest.raises(s.TrialError,match='Hooked'):
        s.validate_pool(hooked,TOKEN,'0x'+keccak(encode([s.POOL_TYPE],[hooked])).hex())


def test_preflight_rpc_cannot_send():
    with pytest.raises(ValueError,match='read-only'):
        s.SwapRpc('https://example.com')('eth_sendRawTransaction',[])


def test_wrong_chain_stops_before_other_reads():
    calls=[]
    def rpc(m,p):
        calls.append(m)
        return '0x1'
    with pytest.raises(s.TrialError,match='chain mismatch'):
        s.check_network(rpc,WALLET)
    assert calls==['eth_chainId']


def test_quotes_cannot_use_stale_data_or_exceed_budget():
    market={'observed_at':time.time()-20}
    with pytest.raises(s.TrialError,match='stale'):
        s.plan(None,WALLET,TOKEN,'buy',Decimal(3),KEY,market)
    market['observed_at']=time.time()
    with pytest.raises(s.TrialError,match=r'\$2 to \$5'):
        s.plan(None,WALLET,TOKEN,'buy',Decimal(6),KEY,market)


def test_unknown_broadcast_has_durable_hash_and_blocks_retry(monkeypatch,tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('ROBINHOOD_LIVE_TEST_ENABLED','true')
    monkeypatch.setenv('AGENT_LIVE_KILL_SWITCH','false')
    account=Account.create()
    wallet=account.address.lower()
    journal={'transactions':[]}
    s.claim(journal)
    monkeypatch.setattr(s,'estimate',lambda *a:(21000,1))
    def rpc(m,p):
        if m=='eth_chainId':return hex(s.CHAIN_ID)
        if m=='eth_getTransactionCount':return '0x0'
        if m=='eth_sendRawTransaction':
            disk=json.loads(s.JOURNAL.read_text())
            expected='0x'+keccak(bytes.fromhex(p[0][2:])).hex()
            assert disk['transactions'][0]['hash']==expected
            assert disk['transactions'][0]['status']=='SUBMISSION_UNKNOWN'
            raise TimeoutError('unknown outcome')
        raise AssertionError(m)
    with pytest.raises(TimeoutError):
        s.send(rpc,{'from':wallet,'to':s.ROUTER,'value':'0x0','data':'0x'},account.key,Decimal(2000),journal,'SWAP')
    with pytest.raises(s.TrialError,match='already attempted'):
        s.claim({'transactions':[]})


def test_expiry_during_simulation_prevents_sign_and_broadcast(monkeypatch):
    monkeypatch.setenv('ROBINHOOD_LIVE_TEST_ENABLED','true')
    monkeypatch.setenv('AGENT_LIVE_KILL_SWITCH','false')
    monkeypatch.setattr(s,'estimate',lambda *a:(21000,1))
    calls=[]
    def rpc(m,p):
        calls.append(m)
        return hex(s.CHAIN_ID) if m=='eth_chainId' else '0x0'
    with pytest.raises(s.TrialError,match='expired during simulation'):
        s.send(rpc,{'from':WALLET},'unused',Decimal(2000),{'transactions':[]},'SWAP',time.time()-1)
    assert 'eth_sendRawTransaction' not in calls


def test_receipt_transfer_proves_token_delivery_and_debit():
    def receipt(src,dst,amount):
        return {'logs':[{'address':TOKEN,'topics':[s.TRANSFER,'0x'+src[2:].zfill(64),'0x'+dst[2:].zfill(64)],'data':'0x'+encode(['uint256'],[amount]).hex()}]}
    s.verify_transfers(receipt(s.MANAGER,WALLET,50),TOKEN,WALLET,{'buy':True,'minimum':49})
    with pytest.raises(s.TrialError,match='below minimum'):
        s.verify_transfers(receipt(s.MANAGER,WALLET,48),TOKEN,WALLET,{'buy':True,'minimum':49})
    s.verify_transfers(receipt(WALLET,s.MANAGER,50),TOKEN,WALLET,{'buy':False,'amount':50})
    with pytest.raises(s.TrialError,match='differs'):
        s.verify_transfers(receipt(WALLET,s.MANAGER,51),TOKEN,WALLET,{'buy':False,'amount':50})


def test_sell_permit_is_exact_short_lived(monkeypatch):
    account=Account.create()
    monkeypatch.setattr(s,'call',lambda *a,**kw:(0,0,7))
    permit=s.permit_data(None,account.address.lower(),TOKEN,123,999,account.key)
    details,signature=decode(['((address,uint160,uint48,uint48),address,uint256)','bytes'],permit)
    assert details==((TOKEN,123,999,7),s.ROUTER,999)
    assert len(signature)==65


def test_pool_log_scan_is_bounded_and_returns_verified_key(monkeypatch):
    monkeypatch.setattr(s,'block_at',lambda rpc,ts,head:100 if ts==880 else 250)
    requests=[]
    def rpc(m,p):
        requests.append(p[0])
        if p[0]['fromBlock']=='0x64':return []
        return [{'address':s.MANAGER,'topics':[s.INIT_TOPIC,POOL,'0x'+s.ZERO[2:].zfill(64),'0x'+TOKEN[2:].zfill(64)],
                 'data':'0x'+encode(['uint24','int24','address','uint160','int24'],[3000,60,s.ZERO,1,0]).hex()}]
    assert s.pool_key(rpc,TOKEN,{'created':1000,'pool_id':POOL},300)==KEY
    assert requests[0]['fromBlock']=='0x64' and requests[0]['toBlock']==hex(199)
    assert requests[1]['fromBlock']==hex(200) and requests[1]['toBlock']==hex(250)


@pytest.mark.parametrize('execute',[False,True])
def test_buy_trial_end_to_end_mocked_chain(monkeypatch,tmp_path,capsys,execute):
    from types import SimpleNamespace
    monkeypatch.chdir(tmp_path)
    account=Account.create()
    wallet=account.address.lower()
    monkeypatch.setenv('ROBINHOOD_WALLET_ADDRESS',wallet)
    monkeypatch.setenv('ROBINHOOD_LIVE_TEST_ENABLED','true')
    monkeypatch.setenv('AGENT_LIVE_KILL_SWITCH','false')
    monkeypatch.setattr(s,'_load_dotenv',lambda:None)
    monkeypatch.setattr(s,'keychain',lambda:SimpleNamespace(get_password=lambda *a:account.key.hex()))
    monkeypatch.setattr(s,'check_network',lambda *a:123)
    monkeypatch.setattr(s,'pool_key',lambda *a:KEY)
    monkeypatch.setattr(s,'discover',lambda *a:{'pool_id':POOL,'created':1,'token_usd':Decimal(1),'native_usd':Decimal(2000),'observed_at':time.time()})
    calls=[]
    state={'hash':None}
    def rpc(method,params):
        calls.append(method)
        if method=='eth_chainId':return hex(s.CHAIN_ID)
        if method=='eth_getTransactionCount':return '0x0'
        if method=='eth_getBalance':return hex(10**18)
        if method=='eth_gasPrice':return hex(1000000)
        if method=='eth_estimateGas':return hex(100000)
        if method=='eth_sendRawTransaction':
            state['hash']='0x'+keccak(bytes.fromhex(params[0][2:])).hex()
            return state['hash']
        if method=='eth_getBlockByNumber':return {'hash':'0xabc'}
        if method=='eth_getTransactionReceipt':
            return {'transactionHash':state['hash'],'from':wallet,'to':s.ROUTER,'status':'0x1','blockNumber':'0x7b','blockHash':'0xabc',
                'logs':[{'address':TOKEN,'topics':[s.TRANSFER,'0x'+s.MANAGER[2:].zfill(64),'0x'+wallet[2:].zfill(64)],'data':'0x'+encode(['uint256'],[3000000]).hex()}]}
        if method=='eth_call':
            data=params[0]['data']
            if data.startswith(s.calldata('decimals()')[:10]):return '0x'+encode(['uint256'],[6]).hex()
            if data.startswith(s.calldata('balanceOf(address)',['address'],[wallet])[:10]):
                return '0x'+encode(['uint256'],[3000000 if state['hash'] else 0]).hex()
            if params[0]['to']==s.QUOTER:
                _,buy,amount,_=decode([f'({s.POOL_TYPE},bool,uint128,bytes)'],bytes.fromhex(data[10:]))[0]
                result=3000000 if buy else 1500000000000000
                return '0x'+encode(['uint256','uint256'],[result,100000]).hex()
            if params[0]['to']==s.ROUTER:return '0x'
        raise AssertionError((method,params))
    monkeypatch.setattr(s,'SwapRpc',lambda *a:rpc)
    monkeypatch.setattr(s,'BroadcastRpc',lambda *a:rpc)
    monkeypatch.setattr('sys.argv',['trial','--token',TOKEN,'--side','buy','--usd','3']+(['--execute'] if execute else []))
    s.main()
    result=json.loads(capsys.readouterr().out)
    assert result['broadcast'] is execute
    assert result['swap_simulated'] is True
    if execute:
        assert calls.count('eth_sendRawTransaction')==1
        journal=json.loads(s.JOURNAL.read_text())
        assert journal['token_balance_after']=='3000000'
        assert journal['transactions'][0]['hash']==state['hash']
    else:
        assert 'eth_sendRawTransaction' not in calls
        assert not s.JOURNAL.exists()


def test_no_native_gas_fails_before_simulation():
    calls=[]
    def rpc(m,p):
        calls.append(m)
        return '0x0'
    with pytest.raises(s.TrialError,match='Insufficient native ETH'):
        s.estimate(rpc,{'from':WALLET,'value':'0x0'},Decimal(2000))
    assert calls==['eth_getBalance']


def test_sell_approval_and_swap_use_two_bounded_transactions(monkeypatch,tmp_path,capsys):
    from types import SimpleNamespace
    monkeypatch.chdir(tmp_path)
    account=Account.create()
    wallet=account.address.lower()
    monkeypatch.setenv('ROBINHOOD_WALLET_ADDRESS',wallet)
    monkeypatch.setenv('ROBINHOOD_LIVE_TEST_ENABLED','true')
    monkeypatch.setenv('AGENT_LIVE_KILL_SWITCH','false')
    monkeypatch.setattr(s,'_load_dotenv',lambda:None)
    monkeypatch.setattr(s,'keychain',lambda:SimpleNamespace(get_password=lambda *a:account.key.hex()))
    monkeypatch.setattr(s,'check_network',lambda *a:123)
    monkeypatch.setattr(s,'pool_key',lambda *a:KEY)
    monkeypatch.setattr(s,'discover',lambda *a:{'pool_id':POOL,'created':1,'token_usd':Decimal(1),'native_usd':Decimal(2000),'observed_at':time.time()})
    state={'hashes':[]}
    def rpc(method,params):
        if method=='eth_chainId':return hex(s.CHAIN_ID)
        if method=='eth_getTransactionCount':return hex(len(state['hashes']))
        if method=='eth_getBalance':return hex(10**18)
        if method=='eth_gasPrice':return hex(1000000)
        if method=='eth_estimateGas':return hex(100000)
        if method=='eth_sendRawTransaction':
            from eth_account._utils.legacy_transactions import Transaction
            tx=Transaction.from_bytes(bytes.fromhex(params[0][2:]))
            if not state['hashes']:
                assert bytes(tx.to).hex()==TOKEN[2:]
                spender,amount=decode(['address','uint256'],bytes(tx.data)[4:])
                assert spender==s.PERMIT2 and amount==3000000
            else:
                assert bytes(tx.to).hex()==s.ROUTER[2:]
                commands,inputs,_=decode(['bytes','bytes[]','uint256'],bytes(tx.data)[4:])
                assert commands==b'\x0a\x10'
                permit,_=decode(['((address,uint160,uint48,uint48),address,uint256)','bytes'],inputs[0])
                assert permit[0][1]==3000000
            h='0x'+keccak(bytes.fromhex(params[0][2:])).hex()
            state['hashes'].append(h)
            return h
        if method=='eth_getBlockByNumber':return {'hash':'0xabc'}
        if method=='eth_getTransactionReceipt':
            swap=len(state['hashes'])==2
            return {'transactionHash':state['hashes'][-1],'from':wallet,'to':s.ROUTER if swap else TOKEN,'status':'0x1','blockNumber':'0x7b','blockHash':'0xabc',
                'logs':[{'address':TOKEN,'topics':[s.TRANSFER,'0x'+wallet[2:].zfill(64),'0x'+s.MANAGER[2:].zfill(64)],'data':'0x'+encode(['uint256'],[3000000]).hex()}] if swap else []}
        if method=='eth_call':
            to=params[0]['to'];data=params[0]['data']
            if to==s.PERMIT2:return '0x'+encode(['uint160','uint48','uint48'],[0,0,0]).hex()
            if data[:10]==s.calldata('decimals()')[:10]:return '0x'+encode(['uint256'],[6]).hex()
            if data[:10]==s.calldata('balanceOf(address)',['address'],[wallet])[:10]:
                return '0x'+encode(['uint256'],[7000000 if len(state['hashes'])==2 else 10000000]).hex()
            if data[:10]==s.calldata('allowance(address,address)',['address','address'],[wallet,s.PERMIT2])[:10]:
                return '0x'+encode(['uint256'],[3000000 if state['hashes'] else 0]).hex()
            if to==s.QUOTER:return '0x'+encode(['uint256','uint256'],[1500000000000000,100000]).hex()
            if to==s.ROUTER or data[:10]==s.calldata('approve(address,uint256)',['address','uint256'],[s.PERMIT2,3000000])[:10]:return '0x'
        raise AssertionError((method,params))
    monkeypatch.setattr(s,'BroadcastRpc',lambda *a:rpc)
    monkeypatch.setattr('sys.argv',['trial','--token',TOKEN,'--side','sell','--usd','3','--execute'])
    s.main()
    result=json.loads(capsys.readouterr().out)
    assert result['broadcast'] and result['token_balance_after']=='7000000'
    journal=json.loads(s.JOURNAL.read_text())
    assert [tx['kind'] for tx in journal['transactions']]==['EXACT_APPROVAL','SWAP']


def test_discovery_uses_current_endpoint_and_accepts_reverse_orientation(monkeypatch):
    seen=[]
    def response(payload):
        class R:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return b''
        return R()
    payload=[{'chainId':'robinhood','dexId':'uniswap','labels':['v4'],
        'baseToken':{'address':s.ZERO},'quoteToken':{'address':TOKEN},
        'liquidity':{'usd':60000},'priceUsd':'2','priceNative':'0.001',
        'pairCreatedAt':1000000,'pairAddress':POOL}]
    monkeypatch.setattr(s,'urlopen',lambda request,**kw: (seen.append(request.full_url) or __import__('io').StringIO(__import__('json').dumps(payload))))
    # StringIO provides the context manager used by urlopen.
    market=s.discover(TOKEN)
    assert seen==['https://api.dexscreener.com/token-pairs/v1/robinhood/'+TOKEN]
    assert market['native_usd']==Decimal('0.002') and market['native_currency']==s.ZERO


def test_discovery_falls_back_to_legacy_and_reports_no_matching_pool(monkeypatch):
    calls=[]
    def open_(request,**kw):
        calls.append(request.full_url)
        if len(calls)==1: raise OSError('unavailable')
        import io
        return io.StringIO(json.dumps({'pairs':[]}))
    monkeypatch.setattr(s,'urlopen',open_)
    with pytest.raises(s.TrialError,match='No supported ETH'):
        s.discover(TOKEN)
    assert len(calls)==2


def test_discovery_uses_current_endpoint_and_accepts_reverse_orientation(monkeypatch):
    import io
    seen=[]
    payload=[{'chainId':'robinhood','dexId':'uniswap','labels':['v4'],
        'baseToken':{'address':s.ZERO},'quoteToken':{'address':TOKEN},
        'liquidity':{'usd':60000},'priceUsd':'2','priceNative':'0.001',
        'pairCreatedAt':1000000,'pairAddress':POOL}]
    monkeypatch.setattr(s,'urlopen',lambda request,**kw: (seen.append(request.full_url) or io.BytesIO(json.dumps(payload).encode())))
    market=s.discover(TOKEN)
    assert seen==['https://api.dexscreener.com/token-pairs/v1/robinhood/'+TOKEN]
    assert market['native_usd']==Decimal('0.002') and market['native_currency']==s.ZERO


def test_discovery_falls_back_to_legacy_and_reports_no_matching_pool(monkeypatch):
    import io
    calls=[]
    def open_(request,**kw):
        calls.append(request.full_url)
        if len(calls)==1: raise OSError('unavailable')
        return io.BytesIO(json.dumps({'pairs':[]}).encode())
    monkeypatch.setattr(s,'urlopen',open_)
    with pytest.raises(s.TrialError,match='No supported ETH'):
        s.discover(TOKEN)
    assert len(calls)==2


def test_discovery_accepts_weth_and_buy_wraps_before_v4_swap(monkeypatch):
    import io
    key=(s.WETH,TOKEN,3000,60,s.ZERO)
    pool='0x'+keccak(encode([s.POOL_TYPE],[key])).hex()
    payload=[{'chainId':'robinhood','dexId':'uniswap','labels':['v4'],
        'baseToken':{'address':TOKEN},'quoteToken':{'address':s.WETH},
        'liquidity':{'usd':60000},'priceUsd':'2','priceNative':'0.001',
        'pairCreatedAt':1000000,'pairAddress':pool}]
    monkeypatch.setattr(s,'urlopen',lambda *a,**kw:io.BytesIO(json.dumps(payload).encode()))
    market=s.discover(TOKEN)
    assert market['native_currency']==s.WETH and market['native_usd']==Decimal('2000')
    s.validate_pool(key,TOKEN,pool,market['native_currency'])
    commands,inputs,_=decode(['bytes','bytes[]','uint256'],bytes.fromhex(s.swap_data(key,True,1000,900,999)[10:]))
    assert commands==b'\x0b\x10\x04'
    assert decode(['address','uint256'],inputs[0])==('0x'+'00'*19+'02',1000)


def test_weth_sell_unwraps_to_wallet_after_v4_swap():
    key=(s.WETH,TOKEN,3000,60,s.ZERO)
    commands,inputs,_=decode(['bytes','bytes[]','uint256'],bytes.fromhex(s.swap_data(key,False,1000,900,999,b'permit')[10:]))
    assert commands==b'\x0a\x10\x0c'
    assert inputs[0]==b'permit'
    assert decode(['address','uint256'],inputs[2])==('0x'+'00'*19+'01',900)
