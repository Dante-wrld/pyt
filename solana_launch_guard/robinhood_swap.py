"""Bounded, one-shot Robinhood native-ETH/Uniswap-v4 swap trial.

No background trading. Preflight never broadcasts. Exact pool keys come from
PoolManager Initialize logs, not guessed fees. See docs/robinhood-swap-trial.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import time
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, urlopen

import certifi
from eth_abi import encode, decode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_checksum_address

from .config import _load_dotenv
from .robinhood_setup import CHAIN_ID, DEFAULT_RPC, SERVICE, ReadOnlyRpc, address, keychain, key_address

class TrialError(ValueError):
    pass


ZERO = '0x' + '00' * 20
ROUTER = '0x204faca1764b154221e35c0d20abb3c525710498'  # UniversalRouter 2.1.2
MANAGER = '0x8366a39cc670b4001a1121b8f6a443a643e40951'
QUOTER = '0x8dc178efb8111bb0973dd9d722ebeff267c98f94'
PERMIT2 = '0x000000000022d473030f116ddee9f6b43ac78ba3'
POOL_TYPE = '(address,address,uint24,int24,address)'
INIT_TOPIC = '0x' + keccak(text='Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)').hex()
TRANSFER = '0x' + keccak(text='Transfer(address,address,uint256)').hex()
JOURNAL = Path('launch_guard_robinhood_trial.json')


class SwapRpc(ReadOnlyRpc):
    def __call__(self, method, params):
        try:
            return super().__call__(method, params)
        except ValueError as exc:
            raise TrialError(str(exc)) from None

    METHODS = ReadOnlyRpc.METHODS | {
        'eth_getLogs', 'eth_getBlockByNumber', 'eth_getTransactionCount',
        'eth_gasPrice', 'eth_estimateGas', 'eth_getTransactionReceipt',
    }


class BroadcastRpc(SwapRpc):
    METHODS = SwapRpc.METHODS | {'eth_sendRawTransaction'}


def calldata(signature, types=(), values=()):
    return '0x' + (keccak(text=signature)[:4] + encode(types, values)).hex()


def call(rpc, to, signature, types=(), values=(), returns=('uint256',), block='latest'):
    raw = rpc('eth_call', [{'to': to, 'data': calldata(signature, types, values)}, block])
    return decode(returns, bytes.fromhex(raw[2:]))


def finite_positive(value):
    n = Decimal(str(value))
    if not n.is_finite() or n <= 0:
        raise TrialError('Expected a finite positive amount')
    return n


def _market_pairs(token):
    """Use Dexscreener's token-pairs endpoint, with the legacy endpoint as fallback."""
    urls = [
        'https://api.dexscreener.com/token-pairs/v1/robinhood/' + token,
        'https://api.dexscreener.com/latest/dex/tokens/' + token,
    ]
    for url in urls:
        try:
            with urlopen(Request(url, headers={'User-Agent': 'LaunchGuard/1'}), timeout=15,
                         context=ssl.create_default_context(cafile=certifi.where())) as response:
                payload = json.load(response)
            pairs = payload.get('pairs') if isinstance(payload, dict) else payload
            if isinstance(pairs, list):
                return pairs
        except Exception:
            pass
    raise TrialError('Dexscreener market data is unavailable; no trade planned')


def discover(token):
    # Dexscreener may list native ETH as either base or quote. PoolKey validation
    # later requires the canonical address(0)/token ordering.
    matches = []
    for pair in _market_pairs(token):
        if not isinstance(pair, dict) or pair.get('chainId') != 'robinhood' or pair.get('dexId') != 'uniswap' or 'v4' not in pair.get('labels', []):
            continue
        base = str(pair.get('baseToken', {}).get('address', '')).lower()
        quote = str(pair.get('quoteToken', {}).get('address', '')).lower()
        if (base, quote) not in {(token, ZERO), (ZERO, token)}:
            continue
        try:
            liquidity = finite_positive(pair['liquidity']['usd'])
            token_usd = finite_positive(pair['priceUsd'])
            price_native = finite_positive(pair['priceNative'])
            created = int(pair['pairCreatedAt']) // 1000
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
        native_usd = token_usd / price_native if base == token else token_usd * price_native
        pool_id = str(pair.get('pairAddress', '')).lower()
        if re.fullmatch('0x[0-9a-f]{64}', pool_id):
            matches.append((liquidity, {'pool_id': pool_id, 'created': created,
                'token_usd': token_usd, 'native_usd': native_usd, 'observed_at': time.time(),
                'orientation': 'token/native' if base == token else 'native/token'}))
    if not matches:
        raise TrialError('No native-ETH Uniswap v4 pool was returned for this token; no trade planned')
    liquidity, market = max(matches, key=lambda item: item[0])
    if liquidity < 50000:
        raise TrialError('Pool liquidity is below the $50,000 trial floor')
    return market

def block_at(rpc, timestamp, high):
    low = 0
    while low < high:
        middle = (low + high) // 2
        block = rpc('eth_getBlockByNumber', [hex(middle), False])
        if int(block['timestamp'], 16) < timestamp:
            low = middle + 1
        else:
            high = middle
    return low


def pool_key(rpc, token, market, head):
    start = block_at(rpc, market['created'] - 120, head)
    end = min(head, block_at(rpc, market['created'] + 120, head))
    if end - start > 10000:
        raise TrialError('Pool discovery window exceeds the bounded scan')
    for first in range(start, end + 1, 100):
        logs = rpc('eth_getLogs', [{'address': MANAGER, 'fromBlock': hex(first),
                    'toBlock': hex(min(first + 99, end)), 'topics': [INIT_TOPIC, market['pool_id']]}])
        if not isinstance(logs, list):
            raise TrialError('Invalid pool log response')
        for log in logs:
            if log.get('removed') or log.get('address', '').lower() != MANAGER:
                continue
            topics = log.get('topics', [])
            if len(topics) != 4 or [t.lower() for t in topics[:2]] != [INIT_TOPIC, market['pool_id']]:
                raise TrialError('Unexpected Initialize log')
            c0, c1 = ('0x' + t[-40:].lower() for t in topics[2:])
            fee, spacing, hooks, _, _ = decode(['uint24','int24','address','uint160','int24'], bytes.fromhex(log['data'][2:]))
            key = (c0, c1, fee, spacing, hooks.lower())
            validate_pool(key, token, market['pool_id'])
            return key
    raise TrialError('Pool Initialize event not found; no fee/hook parameters guessed')


def validate_pool(key, token, pool_id):
    if '0x' + keccak(encode([POOL_TYPE], [key])).hex() != pool_id:
        raise TrialError('Pool key hash mismatch')
    if key[0] != ZERO or key[1] != token:
        raise TrialError('Only native ETH/token pools are supported')
    if key[4] != ZERO:
        raise TrialError('Hooked pool requires a separate adapter review; trial blocked')
    if not 0 < key[2] <= 10000 or not 0 < key[3] <= 32767:
        raise TrialError('Unsupported pool fee or tick spacing')


def check_network(rpc, wallet):
    if int(rpc('eth_chainId', []), 16) != CHAIN_ID:
        raise TrialError('RPC chain mismatch: expected Robinhood 4663')
    head = rpc('eth_getBlockByNumber', ['latest', False])
    if not -5 <= time.time() - int(head['timestamp'], 16) <= 30:
        raise TrialError('RPC head is stale')
    code = bytes.fromhex(rpc('eth_getCode', [wallet, 'latest'])[2:])
    if code and not (len(code) == 23 and code.startswith(bytes.fromhex('ef0100'))):
        raise TrialError('Wallet is not an EOA or EIP-7702 delegated EOA')
    for target in (ROUTER, MANAGER, QUOTER, PERMIT2):
        code = bytes.fromhex(rpc('eth_getCode', [target, 'latest'])[2:])
        if not code:
            raise TrialError('Required Uniswap deployment is missing')
        if target == ROUTER and len(code) != 24380:
            raise TrialError('Unexpected UniversalRouter 2.1.2 runtime length')
    for target in (ROUTER, QUOTER):
        if call(rpc, target, 'poolManager()', returns=('address',))[0].lower() != MANAGER:
            raise TrialError('Router/quoter PoolManager mismatch')
    return int(head['number'], 16)


def quote(rpc, key, buy, amount):
    result = call(rpc, QUOTER,
        'quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))',
        [f'({POOL_TYPE},bool,uint128,bytes)'], [(key, buy, amount, b'')],
        returns=('uint256','uint256'))[0]
    if not 0 < result < 2**128:
        raise TrialError('Invalid swap quote')
    return result


def swap_data(key, buy, amount, minimum, deadline, permit=None):
    # Interface pinned to the 2.1.2 release dependency; never use upstream main.
    swap = encode([f'({POOL_TYPE},bool,uint128,uint128,uint256,bytes)'], [(key, buy, amount, minimum, minimum * 10**36 // amount, b'')])
    incoming, outgoing = (key[0], key[1]) if buy else (key[1], key[0])
    actions = encode(['bytes','bytes[]'], [bytes.fromhex('060c0f'), [swap,
        encode(['address','uint256'], [incoming, amount]),
        encode(['address','uint256'], [outgoing, minimum])]])
    commands, inputs = b'\x10', [actions]
    if buy:
        # Return any unspent native input after a partial pool fill. Address 1
        # is the Universal Router's mapped MSG_SENDER recipient.
        commands += b'\x04'
        inputs.append(encode(['address','address','uint256'], [ZERO, '0x'+'00'*19+'01', 0]))
    if permit:
        commands, inputs = b'\x0a\x10', [permit, actions]
    return calldata('execute(bytes,bytes[],uint256)', ['bytes','bytes[]','uint256'], [commands, inputs, deadline])


def permit_data(rpc, wallet, token, amount, deadline, secret):
    _, _, nonce = call(rpc, PERMIT2, 'allowance(address,address,address)',
        ['address','address','address'], [wallet, token, ROUTER], returns=('uint160','uint48','uint48'))
    details = {'token': token, 'amount': amount, 'expiration': deadline, 'nonce': nonce}
    message = {'types': {
        'EIP712Domain': [{'name':'name','type':'string'}, {'name':'chainId','type':'uint256'}, {'name':'verifyingContract','type':'address'}],
        'PermitDetails': [{'name':'token','type':'address'}, {'name':'amount','type':'uint160'}, {'name':'expiration','type':'uint48'}, {'name':'nonce','type':'uint48'}],
        'PermitSingle': [{'name':'details','type':'PermitDetails'}, {'name':'spender','type':'address'}, {'name':'sigDeadline','type':'uint256'}]},
        'primaryType':'PermitSingle', 'domain':{'name':'Permit2','chainId':CHAIN_ID,'verifyingContract':PERMIT2},
        'message':{'details':details,'spender':ROUTER,'sigDeadline':deadline}}
    signature = Account.sign_message(encode_typed_data(full_message=message), secret).signature
    return encode(['((address,uint160,uint48,uint48),address,uint256)','bytes'],
        [((token, amount, deadline, nonce), ROUTER, deadline), bytes(signature)])


def plan(rpc, wallet, token, side, usd, key, market, secret=None, fixed_amount=None):
    if time.time() - market['observed_at'] > 15:
        raise TrialError('Market data is stale; rerun preflight')
    if not Decimal('2') <= usd <= Decimal('5'):
        raise TrialError('Trial amount must be $2 to $5')
    decimals = call(rpc, token, 'decimals()')[0]
    if decimals > 36:
        raise TrialError('Unsupported token decimals')
    buy = side == 'buy'
    amount = int(usd / (market['native_usd'] if buy else market['token_usd']) * (10**18 if buy else 10**decimals))
    if fixed_amount is not None:
        amount = fixed_amount
        current_usd = Decimal(amount) / (10**18 if buy else 10**decimals) * (market['native_usd'] if buy else market['token_usd'])
        if not Decimal('2') <= current_usd <= Decimal('5'):
            raise TrialError('Approved amount moved outside trial USD limits')
        usd = current_usd
    if not 0 < amount < 2**128:
        raise TrialError('Invalid input amount')
    expected = quote(rpc, key, buy, amount)
    # Reject material quote divergence from discovery prices, including unexpected premiums.
    output_usd = Decimal(expected) / (10**decimals if buy else 10**18) * (market['token_usd'] if buy else market['native_usd'])
    if not usd * Decimal('.97') <= output_usd <= usd * Decimal('1.03'):
        raise TrialError('Quote differs from market value by more than 3%')
    if buy and quote(rpc, key, False, expected) < amount * 95 // 100:
        raise TrialError('Reverse quote loses more than 5% before gas')
    minimum = expected * 99 // 100
    if not buy and Decimal(minimum) / 10**18 * market['native_usd'] < 2:
        raise TrialError('Minimum sell proceeds fall below $2')
    balance = call(rpc, token, 'balanceOf(address)', ['address'], [wallet])[0]
    if not buy and balance < amount:
        raise TrialError('Insufficient token balance for requested sell')
    allowance = call(rpc, token, 'allowance(address,address)', ['address','address'], [wallet, PERMIT2])[0] if not buy else 0
    approval_required = not buy and allowance < amount
    if approval_required and allowance:
        raise TrialError('Existing nonzero insufficient approval needs separate review')
    deadline = int(time.time()) + 90
    permit = permit_data(rpc, wallet, token, amount, deadline, secret) if not buy and secret else None
    tx = {'from': wallet, 'to': ROUTER, 'value': hex(amount if buy else 0),
          'data': swap_data(key, buy, amount, minimum, deadline, permit)}
    return {'tx':tx,'amount':amount,'minimum':minimum,'expected':expected,'buy':buy,
            'approval_required':approval_required,'deadline':deadline,'token_balance_before':balance,
            'native_balance_before':int(rpc('eth_getBalance',[wallet,'latest']),16),
            'output_usd':str(output_usd),'observed_at':market['observed_at']}


def enabled():
    if os.getenv('ROBINHOOD_LIVE_TEST_ENABLED','false').lower() != 'true':
        raise TrialError('ROBINHOOD_LIVE_TEST_ENABLED is false')
    if os.getenv('AGENT_LIVE_KILL_SWITCH','true').lower() != 'false':
        raise TrialError('AGENT_LIVE_KILL_SWITCH is active')


def write_journal(payload):
    temporary = JOURNAL.with_suffix('.tmp')
    with open(temporary, 'w') as stream:
        json.dump(payload, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, JOURNAL)
    fd = os.open(JOURNAL.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def claim(payload):
    if JOURNAL.exists():
        raise TrialError('Robinhood trial already attempted; inspect journal, do not reset it')
    try:
        fd = os.open(str(JOURNAL)+'.claim', os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    except FileExistsError:
        raise TrialError('Robinhood trial already claimed; do not retry') from None
    os.close(fd)
    write_journal(payload)


def estimate(rpc, tx, native_usd, spent=0):
    if int(rpc('eth_getBalance', [tx['from'], 'pending']), 16) <= int(tx['value'], 16):
        raise TrialError('Insufficient native ETH for trade plus gas on Robinhood Chain')
    rpc('eth_call', [tx, 'pending'])
    gas = (int(rpc('eth_estimateGas', [tx]),16) * 130 + 99) // 100
    price = int(rpc('eth_gasPrice', []),16) * 120 // 100
    if gas <= 0 or price <= 0 or gas > 1500000:
        raise TrialError('Invalid or excessive gas estimate')
    ceiling = min(10**14, int(Decimal('1') / native_usd * 10**18))
    if spent + gas * price > ceiling:
        raise TrialError('Trial gas ceiling exceeded (at most $1 and 0.0001 ETH)')
    if int(rpc('eth_getBalance',[tx['from'],'pending']),16) < int(tx['value'],16) + gas*price:
        raise TrialError('Insufficient native ETH for trade plus gas on Robinhood Chain')
    return gas, price


def send(rpc, tx, secret, native_usd, journal, label, valid_until=None):
    enabled()
    if int(rpc('eth_chainId', []),16) != CHAIN_ID:
        raise TrialError('Chain changed before signing')
    latest = int(rpc('eth_getTransactionCount',[tx['from'],'latest']),16)
    pending = int(rpc('eth_getTransactionCount',[tx['from'],'pending']),16)
    if latest != pending:
        raise TrialError('Wallet has pending transactions; trial stopped')
    gas, price = estimate(rpc, tx, native_usd, journal.get('gas_reserved_wei',0))
    if valid_until is not None and time.time() > valid_until:
        raise TrialError('Quote expired during simulation; no swap sent')
    enabled()
    signed = Account.sign_transaction({'chainId':CHAIN_ID,'nonce':pending,
        'to':to_checksum_address(tx['to']),'value':int(tx['value'],16),'data':tx['data'],
        'gas':gas,'gasPrice':price}, secret)
    tx_hash = '0x'+bytes(signed.hash).hex()
    journal['transactions'].append({'kind':label,'hash':tx_hash,'status':'SUBMISSION_UNKNOWN'})
    journal['gas_reserved_wei'] = journal.get('gas_reserved_wei',0)+gas*price
    write_journal(journal)  # Durable hash BEFORE sending; never retry a lost response.
    response = rpc('eth_sendRawTransaction',['0x'+bytes(signed.raw_transaction).hex()])
    if not isinstance(response,str) or response.lower() != tx_hash:
        raise TrialError('Broadcast result uncertain; inspect journal transaction hash')
    for _ in range(30):
        receipt = rpc('eth_getTransactionReceipt',[tx_hash])
        if receipt:
            if receipt.get('transactionHash','').lower() != tx_hash:
                raise TrialError('Receipt hash mismatch')
            if receipt.get('from', '').lower() != tx['from'].lower() or receipt.get('to', '').lower() != tx['to'].lower():
                raise TrialError('Receipt sender or recipient mismatch')
            if int(receipt['status'],16) != 1:
                journal['transactions'][-1]['status']='REVERTED'
                write_journal(journal)
                raise TrialError('Transaction reverted; gas may have been spent')
            journal['transactions'][-1].update(status='MINED',block=receipt['blockNumber'])
            write_journal(journal)
            return receipt
        time.sleep(2)
    raise TrialError('Receipt pending; do not repeat execution, inspect recorded hash')


def verify_transfers(receipt, token, wallet, planned):
    net = 0
    for log in receipt.get('logs', []):
        if log.get('address', '').lower() != token:
            continue
        topics = log.get('topics', [])
        if len(topics) != 3 or topics[0].lower() != TRANSFER:
            continue
        value = decode(['uint256'], bytes.fromhex(log['data'][2:]))[0]
        if ('0x' + topics[1][-40:]).lower() == wallet:
            net -= value
        if ('0x' + topics[2][-40:]).lower() == wallet:
            net += value
    if planned['buy'] and net < planned['minimum']:
        raise TrialError('Mined swap output transfers below minimum; review receipt')
    if not planned['buy'] and net != -planned['amount']:
        raise TrialError('Mined swap token debit differs from plan; review receipt')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token',required=True)
    parser.add_argument('--side',choices=['buy','sell'],required=True)
    parser.add_argument('--usd',default='3')
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    journal=None
    claimed=False
    try:
        _load_dotenv()
        wallet=address(os.getenv('ROBINHOOD_WALLET_ADDRESS') or os.getenv('EVM_WALLET_ADDRESS') or '')
        token=address(args.token)
        usd=finite_positive(args.usd)
        rpc=(BroadcastRpc if args.execute else SwapRpc)(os.getenv('ROBINHOOD_RPC_URL') or DEFAULT_RPC)
        if args.execute:
            enabled()
            if JOURNAL.exists() or Path(str(JOURNAL)+'.claim').exists():
                raise TrialError('Trial already attempted; inspect journal, do not reset it')
        head=check_network(rpc,wallet)
        market=discover(token)
        key=pool_key(rpc,token,market,head)
        market=discover(token)  # Log discovery can take time: refresh sizing before quote.
        validate_pool(key,token,market['pool_id'])
        secret=None
        if args.execute or args.side=='sell':
            secret=keychain().get_password(SERVICE,wallet)
            if not secret:
                raise TrialError('No locally stored Robinhood signer')
            key_address(secret,wallet)
        planned=plan(rpc,wallet,token,args.side,usd,key,market,secret)
        result={'chain_id':CHAIN_ID,'wallet':wallet,'token':token,'side':args.side,
                'router':ROUTER,'pool_id':market['pool_id'],'input_raw':str(planned['amount']),
                'expected_output_raw':str(planned['expected']),'minimum_output_raw':str(planned['minimum']),
                'approval_required':planned['approval_required'],'broadcast':False,'swap_simulated':False}
        if not args.execute:
            if planned['approval_required']:
                result['blocker']='Exact token approval is required before swap simulation'
            else:
                gas,price=estimate(rpc,planned['tx'],market['native_usd'])
                result.update(swap_simulated=True,maximum_gas_wei=str(gas*price))
            print(json.dumps(result,indent=2))
            return
        approval={'from':wallet,'to':token,'value':'0x0',
            'data':calldata('approve(address,uint256)',['address','uint256'],[PERMIT2,planned['amount']])} if planned['approval_required'] else None
        # Missing gas or failed initial simulation must not consume the one-time claim.
        estimate(rpc,approval or planned['tx'],market['native_usd'])
        journal={'wallet':wallet,'token':token,'side':args.side,'status':'STARTED','transactions':[]}
        claim(journal)
        claimed=True
        if approval:
            send(rpc,approval,secret,market['native_usd'],journal,'EXACT_APPROVAL')
            # Keep amount fixed after approval; a changed market must never enlarge it.
            market=discover(token)
            validate_pool(key,token,market['pool_id'])
            replacement=plan(rpc,wallet,token,args.side,usd,key,market,secret,fixed_amount=planned['amount'])
            if replacement['approval_required']:
                raise TrialError('Mined approval did not establish the required allowance')
            if replacement['amount'] != planned['amount']:
                raise TrialError('Price changed during approval; approval remains, swap not sent')
            planned=replacement
        if time.time()-planned['observed_at'] > 15 or time.time() > planned['deadline']-30:
            raise TrialError('Quote expired before broadcast')
        receipt=send(rpc,planned['tx'],secret,market['native_usd'],journal,'SWAP',planned['observed_at']+15)
        verify_transfers(receipt, token, wallet, planned)
        block=receipt['blockNumber']
        canonical=rpc('eth_getBlockByNumber',[block,False])
        if canonical['hash'].lower() != receipt['blockHash'].lower():
            raise TrialError('Receipt block changed; review journal for a reorganization')
        after=call(rpc,token,'balanceOf(address)',['address'],[wallet],block=block)[0]
        native_after=int(rpc('eth_getBalance',[wallet,block]),16)
        journal.update(status='MINED_REVIEW_BALANCES',token_balance_before=str(planned['token_balance_before']),
                       token_balance_after=str(after),native_balance_before=str(planned['native_balance_before']),
                       native_balance_after=str(native_after),receipt=receipt)
        write_journal(journal)
        result.update(broadcast=True,swap_simulated=True,status=journal['status'],
                      transaction_hash=receipt['transactionHash'],token_balance_after=str(after),
                      native_balance_after=str(native_after))
        print(json.dumps(result,indent=2))
    except Exception as exc:
        if claimed and journal is not None and JOURNAL.exists():
            journal['status']='STOPPED_REVIEW_JOURNAL'
            write_journal(journal)
        # Provider errors are redacted by Rpc. Do not expose arbitrary SDK/key errors.
        message=str(exc) if isinstance(exc,TrialError) else 'Robinhood trial failed (provider, ABI or signer error); inspect journal before retrying'
        raise SystemExit(message) from None


if __name__=='__main__':
    main()
