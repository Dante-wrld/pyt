import asyncio
import io
import json
import urllib.error
import pytest
from solana_launch_guard.multichain import EvmRpc, EvmWalletWatcher, LogRangeLimitError


def test_log_range_error_is_redacted(monkeypatch):
    body={'error':{'code':-32602,'message':'secret URL: block range is too large'}}
    monkeypatch.setattr('urllib.request.urlopen',lambda *a,**k:io.BytesIO(json.dumps(body).encode()))
    with pytest.raises(LogRangeLimitError) as e:
        EvmRpc('https://example.com/secret')._request('eth_getLogs',[])
    assert 'secret' not in str(e.value)


def test_http_denial_is_not_range_error(monkeypatch):
    def fail(*a,**k): raise urllib.error.HTTPError('secret',403,'secret',{},None)
    monkeypatch.setattr('urllib.request.urlopen',fail)
    with pytest.raises(ConnectionError,match='HTTP 403') as e:
        EvmRpc('https://example.com/secret')._request('eth_getLogs',[])
    assert not isinstance(e.value,LogRangeLimitError)
    assert 'secret' not in str(e.value)


def test_batch_reduction_never_skips_blocks(monkeypatch):
    accepted=[]
    class Rpc:
        async def block_number(self): return 20
        async def transfers(self, **kw):
            lo,hi=kw['from_block'],kw['to_block']
            if hi-lo+1>3: raise LogRangeLimitError()
            accepted.extend(range(lo,hi+1))
            return []
    async def stop(*args): raise asyncio.CancelledError()
    monkeypatch.setattr(asyncio,'sleep',stop)
    async def callback(event): pass
    watcher=EvmWalletWatcher(chain='robinhood',rpc=Rpc(),wallet='test',callback=callback,lookback_blocks=20)
    with pytest.raises(asyncio.CancelledError): asyncio.run(watcher.run_forever())
    assert accepted==list(range(21))
