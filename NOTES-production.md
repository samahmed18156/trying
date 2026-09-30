# Before real money: the five gates, and what we found

This file is the answer to "is the flash-arb bot ready for real trade?" as of
2026-09-29, and the checklist to re-run before answering it again. It is written
to be re-read when you have forgotten everything, by someone with an unshakeable
belief that the maths is probably fine. Read the findings at the bottom first.

**Verdict: do not trade real size yet. Run the micro-drill (gate 5) at most.**
Not because the code is bad — the mechanism is now proven end to end against
real BNB Chain state — but because the measured opportunity is negative, and
that measurement only became trustworthy today.

---

## 1. Fork tests: the contract, on real pools — GREEN

`pytest tests/test_fork.py -q` → **6 passed**

anvil forks BNB Chain mainnet; the contract is compiled with the pinned solc and
deployed onto the fork; the transaction sent is the exact `plan.call_args` the
planner would send. No mocks, no simulated pools.

| test | what it proves |
|---|---|
| `test_live_plan_either_profits_or_reverts_cleanly` | whatever the market is doing, the plan either profits and clears its own floor, or reverts with balances unchanged — never a partial execution |
| `test_manufactured_edge_profits_though_real_pools` | with a manufactured ~2% edge, the round trip profits, the event's `profit` equals the contract's balance change, and the size is what the numbers predict |
| `test_impossible_min_profit_reverts_without_moving_tokens` | an unreachable floor reverts with `Unprofitable`, nothing moves |
| `test_borrowing_from_the_leg2_pool_reverts_with_lok` | the LOK trap is real, and `choose_flash_pool` avoiding it is load-bearing |
| `test_withdraw_is_owner_only_then_moves_exactly_that_amount` | `withdrawTokens` refuses a non-owner (`NotOwner`) and moves exactly the requested amount for the owner |
| `test_preview_flash_fee_matches_the_planner` | the planner's `ceil(amount × tier / 1e6)` equals the pool's own figure, to the wei |

**Two things to know about running it.**

* The fork needs ARCHIVE state, even forking at head, because anvil fetches state
  lazily and the first cold slot a token read touches may be years old. Free
  endpoints that actually serve it: `api.zan.top/bsc-mainnet`,
  `bsc-mainnet.public.blastapi.io`. `bsc-rpc.publicnode.com` refuses
  ("Archive requests require a personal token") and `bsc-dataseed.binance.org`
  says "missing trie node". The fixture probes candidates and picks one that can
  serve; override with `FORK_RPC_URL=...`.
* The profit test MANUFACTURES its edge by writing the V2 pair's reserves
  (`anvil_setStorageAt`, ~2% skew) rather than waiting for a real mispricing —
  otherwise the suite would pass or fail with the market, which is not a test.
  The reserves slot is *discovered* by matching `getReserves()`, never assumed:
  PancakeSwap's pair stores its reserves in slot 8, not Uniswap's 6.
  Note the skew survives exactly one swap — V2's `swap()` ends by syncing
  reserves back to real balances — so the fixture re-applies it per test.

## 2. Profit floor ≥ gas + buffer — DONE

`min_profit` can no longer be 0. `plan_arbitrage` raises it to
`gas cost in quote-token wei + 25%` (buffer settable with `--min-profit-buffer`)
and the CLI can only raise it further. Rationale: `min_profit` is the contract's
only economic gate, and a round trip that clears 1 wei of profit while spending
331k gas of cost is a loss the contract is happy to call a win.

* Gas is converted into the borrow token correctly: WBNB *is* BNB so the
  conversion is the identity; for a non-wrapped quote the planner uses the
  scan's USDT-per-BNB (and if that cannot be found it sets NO floor and SAYS so
  rather than inventing a rate).
* **Gas price: `gas_price_wei` pays 2 gwei while BNB Chain's own `eth_gasPrice`
  is 0.05 gwei — a 40x overpay.** It is the price the floor inherits, so it is
  conservative, but it is real money: 600k gas is ~0.92 USDT at 2 gwei and
  ~0.023 USDT at 0.05. Set `GAS_PRICE_GWEI=0.05` to pay market; leave it unset
  to keep buying priority. Both the floor and the send use the same function, so
  they cannot disagree.

## 3. A week of dry runs, logged — TOOL READY, EVIDENCE COLLECTED (and bad news)

```
python main.py arb survey --network bsc --base WBNB --quote USDT --size 1 \
    --iterations 2000 --interval 12 --duration 604800 --log logs/arb_survey.jsonl
```

`arb survey` never signs, never sends. Each iteration scans, builds the plan the
executor *would* send, prices it exactly as `arb run` does (gas floor included),
and appends one JSONL row: prices, gross bps, flash fee, gas, net bps, whether it
clears, and why not. Rows accumulate across runs.

### What five iterations on BSC mainnet actually showed

| log | gross bps | net bps | clears |
|---|---|---|---|
| `logs/arb_survey_before_buy_cost_fix.jsonl` | +27.9, +29.3 | +10.8, +12.2 | **yes** |
| `logs/arb_survey.jsonl` | −25.2, −18.8, −20.5, −16.2, −25.5 | −42.2 … −33.2 | **no** |

Same pair, same size, same route shape. The only difference is a bug fix
(finding #1 below). **The "profitable" readings were the bug.** On this evidence
there is no edge in the V2→V3 WBNB/USDT route at 1 WBNB size: the route loses
16–25 bps before costs and 33–42 bps after them.

### The cost floor a real edge has to clear (BSC, WBNB/USDT, 1 WBNB)

| item | bps |
|---|---|
| PancakeSwap V2 fee (on leg 1's input) | 25 |
| flash-loan fee (tier-500 lending pool) | 5 |
| V3 tier-100 fee (leg 2) | 1 |
| gas, 600k units @ 2 gwei, ~765 USDT/BNB | ~12 |
| gas @ 0.05 gwei (`GAS_PRICE_GWEI=0.05`) | ~0.3 |
| **total friction** | **~31 bps, ~43 bps with default gas** |

Measured route friction: median −37 bps at 1 WBNB. Cheaper gas does not rescue
a 20 bps negative gross.

## 4. Private submission — IMPLEMENTED, decision recorded

```
python main.py arb run --network bsc --private https://bsc.blockrazor.xyz --execute
```

`send_and_wait(submit_url=...)` broadcasts the signed transaction through any
drop-in private/MEV-protected RPC (BlockRazor, GetBlock+Merkle, a builder
endpoint) while reads and the receipt wait stay on the normal provider. It fails
LOUDLY if the relay is unreachable — it never silently falls back to the public
mempool, because that would defeat the only thing `--private` is for.

The reasoning, the EV arithmetic and the decision rule are in
`docs/ev-private-memo.md`. Short version: the floors mean a searcher cannot take
money *from* you, but they can turn your edge into their edge and your gas into a
fee paid for nothing. At today's measured −20 bps gross there is nothing to
protect and nothing to trade; if the survey ever shows a real edge, that edge is
worth more than the relay's cost and latency, so private submission is the
default posture.

## 5. The micro-drill — RUNBOOK, NOT YET RUN

Only after the survey shows positive net bps for a sustained period (a week of
`arb_survey` rows, not five samples) and a positive median.

1. Deploy on BSC mainnet: `python main.py arb deploy --network bsc` — gas is
   real, ~0.001 BNB.
2. Fund the wallet with **only what the drill needs**. Not the trading float.
   The owner key can withdraw everything the contract holds; treat the wallet as
   the contract's security boundary.
3. Verify the source on BscScan so the deployed bytecode is public (and so the
   constructor cannot have been tampered with).
4. `python main.py arb survey --network bsc --iterations 20 --log logs/pre_drill.jsonl`
   then confirm at least one row with `clears_floor: true`.
5. `python main.py arb plan --network bsc --size <small>` and read every line,
   especially the borrow amount and the floor.
6. `python main.py arb run --network bsc --size <small> --private <relay> --execute`
   at a notional where losing the whole gas cost is irrelevant (~0.1–1 USDT).
7. `python main.py arb status --network bsc` → confirm the profit is in the
   contract.
8. `python main.py arb withdraw --network bsc --token USDT --all --execute` →
   confirm it lands in the wallet. Sweep after every run, always.
9. Record from BscScan: tx hash, gas used, and whether the `ArbitrageExecuted`
   profit matches what the plan predicted. **That comparison is the only test of
   the planner's accuracy that money can buy — and the bug in finding #1 is
   exactly what it would have caught.**

---

## Findings from this pass

### 1. The planner understated the cost of buying by 2× the venue fee (FIXED)

The scan reports, for every venue, what you receive per base SOLD into it
(`dex/fetcher.py` sizes every quote as `amount_in = trade size`). The executor
used the cheap venue's **sell** quote as the **cost of buying** there. Selling 1
base at mid returns `mid×(1−f)`; buying 1 base costs `mid/(1−f) ≈ mid×(1+f)`.

Measured against PancakeSwap V2's live reserves:

```
sell quote (what the planner used) : 753.6651 USDT/WBNB
getAmountIn (the real cost)        : 757.4768 USDT/WBNB
understated by                     : 50.3 bps = 2 × the 25 bps fee
```

Consequences: the loan was sized ~50 bps too small, leg 1 returned ~0.995 base
instead of 1.0, leg 2 sold that for ~50 bps less than promised, and plans the
planner accepted as profitable reverted with `CannotRepay` (observed on the
fork: held 751.12 vs owed 753.58 USDT). Every survey row before the fix was
optimistic by that amount, which is why the "clean" runs looked profitable.

Fix: `plan_arbitrage(..., v2_buy_cost_wei=...)` — the CLI now passes the exact
`getAmountIn` cost from live raw reserves. The planner refuses to silently use
the optimistic number: without a buy cost it adds a note saying the edge is
optimistic and by roughly what.

**This is the finding that matters.** No amount of testnet success would have
surfaced it: on BSC testnet the V2/V3 dislocation was ~20%, which swamps 50 bps.

### 2. Mainnet venue registry was incomplete — BSC/ETH/Base Uniswap V3 had no router (FIXED)

`arb plan --network bsc` died with `(v2=0x10ED…, v3=MISSING)`: the dearest
usable V3 venue was Uniswap V3, whose config had a factory and no router. Added,
each verified on chain (bytecode present, `factory()` returns the configured
factory, selector shape probed — not copied from a blog post):

| chain | router | shape |
|---|---|---|
| BSC | `0xB971eF87ede563556b2ED4b1C0b0019111Dd85d2` | SwapRouter02, 7-field (no deadline) |
| Ethereum | `0xE592427A0AEce92De3Edee1F18E0157C05861564` | SwapRouter, 8-field |
| Base | `0x2626664c2603336E57B271c5C0b26F421741e481` | SwapRouter02, 7-field |

Belt and braces: `best_leg` now excludes legs whose venue has no router, so a
configuration gap can never again masquerade as "no route exists".

### 3. The planner's revert prediction named the wrong guard (FIXED)

Notes said "EXPECTED TO REVERT with Unprofitable". On chain, a route that comes
back short of the loan itself reverts earlier with `CannotRepay` — that guard
runs before the repay, `Unprofitable` after it. The notes now name both and say
which fires when.

### 4. Test suite was not green, and could not have been (FIXED)

`pytest tests/` reported `1 passed, 1 error`: the offline suite's registration
decorator was named `test`, so pytest collected it as an item with a required
`fn` argument → `fixture 'fn' not found`. Renamed to `case`, plus a single
`test_offline_maths_suite` bridge so `pytest tests/` and `main.py selftest`
can never disagree. Now: **94/94 offline, 2 passed under pytest.**

---

## What is still NOT proven

* **That an edge exists at all.** Five samples say no. The survey has to run for
  a week before any answer is worth acting on.
* **Live private-relay behaviour.** The code path is unit-tested for failure and
  the relay protocol is plain `eth_sendRawTransaction`, but no transaction has
  been sent through BlockRazor/GetBlock from here.
* **The seeded opportunity, if one appears.** A persistent mid-price gap against
  a thin pool is a liquidity artefact, not money (see `dex/cross.py`'s own
  notes on the Uniswap V3 0.30% pool: 90x thinner than the 0.01% pool, which is
  *why* it is mispriced). Size into it and the impact eats the edge.
* **Reorg / dropped-transaction handling.** There is none. A transaction that
  does not mine within `--timeout` is reported, not replaced or re-priced.
* **Key management at size.** The owner key can withdraw everything the contract
  ever holds. Do not let that key's exposure exceed the drill.

## Standing rules

1. The contract's profit check is the only protection that counts; everything
   off-chain is advisory. Keep `min_profit` above gas — the planner now enforces
   it and you cannot lower it from the CLI.
2. Never run `--execute` without first seeing the plan's floor printed and a
   `survey` row that clears it.
3. Sweep profit out of the contract after every run (`arb withdraw --all`).
4. Re-run `pytest tests/ -q` and `pytest tests/test_fork.py -q` after ANY change
   to `arb/`, `config.py`, or `contracts/`.
5. If the survey shows positive net bps, believe it only after it survives a
   different size (`--size 0.1` and `--size 10`) and a different hour.
