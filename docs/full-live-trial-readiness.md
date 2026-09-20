# Controlled eight-hour Solana agent trial

Status: **code prepared; no transaction broadcast and no trial started**.
The operator's FROGE full sell and STONK 10% sell preflights passed on their
Mac, but the new runner has not been installed or exercised there. Its buy
simulation, notification configuration and process ownership must be checked
on the Mac before an autonomous session can be called ready.

## Trial constraints

- Hunter has a durable $30 gross-buy ceiling. Each new buy is capped at $5.
- Copy has a reserved $30 gross-buy ceiling but **cannot submit a copy order**
  until a validated leader feed and copy execution path have been reviewed.
- Portfolio may sell eligible existing Solana positions; it cannot buy.
- Gross new-capital ceiling: $60. Sell proceeds appear in cash estimates but
  never raise either $30 gross-buy ceiling.
- BUY requires a fresh BUY_READY recovery, 3/3 confirmations, acceptable
  liquidity, model proposal and explicit live RiskArbiter approval. A signed
  and simulated buy is reserved in SQLite before Jupiter execution; an RPC
  transaction and owner-specific USDC/token balance deltas must match before
  the order counts as confirmed.
- Exits require fresh portfolio or marked-position evidence, model discretion,
  separate live exit arbitration, quote limits, verified signatures,
  simulation and on-chain deltas. The $5 buy cap does not cap exits.
- Hunter profit taking records a separate principal-recovery stage and second
  profit stage; each stage is capped by the existing configured fraction or
  remaining cost basis. Stage flags and fills survive a process restart.
- An unresolved execution halts the supervisor and retains its reservation.
  A process restart cannot reset the eight-hour deadline or budgets, nor can it
  begin a second session automatically.
- A durable maximum of 100 structured model requests protects API credits;
  reaching it halts the autonomous trial even if eight hours have not passed.
- At expiry, the subprocess market/portfolio monitor remains running with
  both automatic live executors disabled. It can continue read-only display
  and alerts. No sale is forced by the timer.

## Installation and non-broadcast check

Apply the reviewed patch to a clean checkout of the same `main` revision;
check the patch with `git apply --check` first. Run the repository's complete
test suite with its installed dependencies.

The following command **only signs and simulates** a $5 USDC-to-WSOL BUY and
a full SELL of the listed wallet-held mint; it does not start the clock:

```bash
cd ~/pyt
.venv/bin/python -m solana_launch_guard.live_trial --preflight \
  --sell-mint YOUR_OWNED_SOLANA_MINT
```

Use an actual owned Solana mint with a valid sell quote. For a
partial sell preflight, add `--sell-decision TAKE_PARTIAL`. A passing buy
simulation proves signer and route construction for USDC/WSOL only. Each
future live candidate must still pass its own fresh buy and reverse-exit quote,
limits and simulation.

## Manual start and stop gates

After the operator reviews a passing readiness report and expressly approves
start, the trial uses `AGENT_LIVE_TRIAL_ENABLED=true` and existing
`AGENT_LIVE_TEST_ENABLED=true`, requires `AGENT_LIVE_KILL_SWITCH=false`,
configured Pushover, correct Keychain signer, configured Solana wallet/Jupiter
API and **exclusive** `AUTO_BUY_LIVE=false`, `AUTO_SELL_LIVE=false`,
`AUTO_REBUY_ENABLED=false`, `AGENT_LIVE_CANARY_ONLY=false`. Stop any other
Launch Guard app monitor. The
eight-hour clock starts only after this explicit command:

```bash
.venv/bin/python -m solana_launch_guard.live_trial --start \
  --confirm START_ONE_EIGHT_HOUR_SOLANA_TRIAL
```

To stop immediately from a second terminal, including while another process
still has old shell environment variables:

```bash
.venv/bin/launch-guard-agents --live-trial-stop
```

To inspect live accounting and the end report:

```bash
.venv/bin/launch-guard-agents --live-trial-status
.venv/bin/launch-guard-agents --live-trial-report
```

The ledger and end report are local private files. Do not commit them or `.env`
or the Keychain contents. Once a trial starts, a new trial requires a separate,
explicit new ledger path and review. Neither `--start` nor a restart resets it.

## Remaining verification before authorizing the start

1. Install the reviewed patch and run the full suite on the Mac. This sandbox
   cannot import the Robinhood EVM test dependencies; the relevant Solana suite
   passes offline but no mainnet buy was simulated here.
2. Run the non-broadcast trial `--preflight` above on the funded Mac, and
   confirm that Pushover credentials exist and the configured wallet matches
   the Keychain signer. The operator's two successful owned-sell preflights
   preceded this new trial code and do not validate its buy route.
3. Ensure no other live executor or app monitor is running before starting.
4. Decide whether on-chain fee reporting is required for the requested
   performance metrics. Unknown portfolio cost basis and unavailable fees
   remain null in the report; no performance claim follows from a simulation.

**Do not execute the start command until these checks pass and the operator
explicitly approves starting this one session.**
