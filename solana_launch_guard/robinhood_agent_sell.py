"""Supervised, one-decision Robinhood Portfolio-agent exit of an owned token."""
from __future__ import annotations

import argparse
import json
import os
import time
from decimal import Decimal

from .agent_live_test import CanaryJournal
from .agents import AgentRole, TradeAction, TradeProposal
from .config import _load_dotenv
from .openai_agents import OpenAIProposalModel
from . import robinhood_swap as swap
from .robinhood_setup import SERVICE, address, key_address, keychain

SESSION = 'launch_guard_robinhood_agent_sell_session.json'


def decide(raw, token, usd):
    """The model may veto a fixed-size sale; it cannot pick another mint or size."""
    proposal = TradeProposal(
        agent_id='portfolio-v1', role=AgentRole.PORTFOLIO_MANAGER,
        action=TradeAction(str(raw['action']).upper()),
        mint=str(raw.get('mint', '')),
        requested_usd=float(raw.get('requested_usd', 0)),
        confidence=float(raw.get('confidence', 0)),
        thesis=str(raw.get('thesis', '')),
    )
    proposal.validate()
    if proposal.action in {TradeAction.HOLD, TradeAction.WATCH}:
        return None
    if proposal.action not in {TradeAction.SELL, TradeAction.TAKE_PARTIAL}:
        raise swap.TrialError('Robinhood agent may only HOLD or sell the owned token')
    if proposal.mint.lower() != token or proposal.confidence < 0.65:
        raise swap.TrialError('Agent sell mint or confidence failed fixed risk checks')
    if abs(Decimal(str(proposal.requested_usd)) - usd) > Decimal('0.01'):
        raise swap.TrialError('Agent cannot change the authorized sell amount')
    return proposal


def preflight(token, usd):
    wallet_source = ('ROBINHOOD_WALLET_ADDRESS' if os.getenv('ROBINHOOD_WALLET_ADDRESS')
                     else 'EVM_WALLET_ADDRESS')
    wallet_raw = os.getenv(wallet_source)
    if not wallet_raw:
        raise swap.TrialError('Set ROBINHOOD_WALLET_ADDRESS to your public Robinhood wallet address in .env')
    try:
        wallet = address(wallet_raw)
    except ValueError:
        raise swap.TrialError(f'{wallet_source} is not a valid nonzero public EVM address') from None
    rpc = swap.SwapRpc(os.getenv('ROBINHOOD_RPC_URL') or swap.DEFAULT_RPC)
    head = swap.check_network(rpc, wallet)
    market = swap.discover(token)
    key = swap.pool_key(rpc, token, market, head)
    market = swap.refresh_market(token, key)
    secret = keychain().get_password(SERVICE, wallet)
    if not secret:
        raise swap.TrialError('No locally stored Robinhood signer')
    key_address(secret, wallet)
    planned = swap.plan(rpc, wallet, token, 'sell', usd, key, market, secret)
    if planned['approval_required']:
        approval = {'from': wallet, 'to': token, 'value': '0x0',
            'data': swap.calldata('approve(address,uint256)', ['address','uint256'],
                                  [swap.PERMIT2, planned['amount']])}
        swap.estimate(rpc, approval, market['native_usd'])
    else:
        swap.estimate(rpc, planned['tx'], market['native_usd'])
    return {
        'wallet': wallet,
        'token': token,
        'requested_usd': str(usd),
        'estimated_proceeds_usd': planned['output_usd'],
        'token_balance_raw': str(planned['token_balance_before']),
        'token_amount_to_sell_raw': str(planned['amount']),
        'market_price_usd': str(market['token_usd']),
        'approval_required': planned['approval_required'],
        'swap_simulated': not planned['approval_required'],
        'observed_at': market['observed_at'],
        'cost_basis': None,
    }


def run(token, usd, execute=False, model=None):
    try:
        token = address(token)
    except ValueError:
        raise swap.TrialError('--token must be the real public Robinhood token contract address (0x plus 40 hex characters)') from None
    usd = swap.finite_positive(usd)
    if not Decimal('2') <= usd <= Decimal('5'):
        raise swap.TrialError('Robinhood agent sell is limited to $2-$5')
    journal = CanaryJournal(SESSION)
    if execute:
        swap.enabled()
        journal.assert_unused()
        if swap.JOURNAL.exists() or swap.JOURNAL.with_name(swap.JOURNAL.name + '.claim').exists():
            raise swap.TrialError('Robinhood trade journal already exists; review it before another attempt')
    view = preflight(token, usd)
    if time.time() - view['observed_at'] > 15:
        raise swap.TrialError('Quote expired before agent review')
    if execute:
        journal.claim({'status':'DECISION_STARTED','at':time.time(),'token':token})
    try:
        agent = model or OpenAIProposalModel()
        if hasattr(agent, 'client'):
            agent.client = agent.client.with_options(max_retries=0)
        raw = agent.propose(role=AgentRole.PORTFOLIO_MANAGER, context={
            'mode':'supervised_robinhood_sell',
            'owned_position':view,
            'constraints':(
                'Return HOLD, SELL, or TAKE_PARTIAL only. A sale must use exactly the '
                'listed token and requested_usd. A quote is not evidence of profit. '
                'Cost basis and net PnL are unknown; do not invent them. You may HOLD. '
                'A required exact approval means swap simulation is pending.'),
        })
        choice = decide(raw, token, usd)
        if execute:
            journal.record({'status':'PROPOSED','proposal':raw,'at':time.time()})
        if choice is None:
            if execute:
                journal.record({'status':'NO_TRADE','at':time.time()})
            return {'agent':'portfolio-v1','chain':'robinhood','broadcast':False,
                    'action':raw['action'],'position':view}
        if not execute:
            return {'agent':'portfolio-v1','chain':'robinhood','broadcast':False,
                    'action':choice.action.value,'position':view,
                    'next_step':'Use --execute for a fresh quote and supervised sale'}
        # This existing executor rechecks chain, owned balance, market, quotes,
        # permit, gas and simulation before its durable single-attempt broadcast.
        swap.main(['--token',token,'--side','sell','--usd',str(usd),'--execute'])
        journal.record({'status':'EXECUTOR_RETURNED','at':time.time()})
    except BaseException:
        if execute:
            journal.record({'status':'STOPPED_REVIEW_REQUIRED','at':time.time()})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token', required=True, help='public Robinhood token contract to review')
    parser.add_argument('--usd', default='3', help='fixed sale amount, from $2 to $5')
    parser.add_argument('--execute', action='store_true', help='permit one real, agent-approved sell')
    args = parser.parse_args()
    _load_dotenv()
    try:
        result = run(args.token, args.usd, args.execute)
        if result is not None:
            print(json.dumps(result, indent=2))
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == '__main__':
    main()
