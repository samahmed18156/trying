# Real-execution runbook — BNB Chain mainnet

**Written 2026-09-30. Read the whole thing before typing anything.**

This is the procedure for going from "the code is finished" to "money is at risk".
It exists because the gap between those two is not a technical one: the contract
is proven, and what remains is a sequence of decisions where each step spends
something real.

---

## The state of things

| | |
|---|---|
| Contract, both leg directions, both flash callbacks | proven on real mainnet forks (9/9), and by `scripts/upgrade_proof.py` |
| Offline maths + preflight | 104/104 |
| Execution costs | **350,338 gas** (V2-first), **286,804 gas** (V3-first); deploy **1,962,419 gas** |
| Current edge | **negative in both directions** — median net −14.85 bps at size 1 |
| Mainnet deployment | **does not exist yet** |
| Mainnet wallet | does not exist yet |

**Gate 1 is the market, and it is not met.** Everything below is preparation for
the day it is. Nothing below should be run to "see if it works" — `arb survey`
exists for that, costs nothing, and has already answered the question 28 times.

---

## Step 0 — the gate that stays shut until the numbers change

```bash
python main.py arb survey --network bsc --base WBNB --quote USDT --size 1 \
    --iterations 5000 --interval 5 --log logs/arb_survey.jsonl
python main.py arb analyze            # negative / sporadic / candidate
```

Nothing else on this page matters until that returns **candidate**, and then only
if it holds for a day at two different sizes. `arb analyze` will not say
"candidate" for a scattering of lucky rows — that is what `sporadic` is for, and
sporadic means no.

Why this is not paranoia: the shortfall is not a rounding error. The measured
spread on WBNB/USDT is roughly the fee it costs to cross, which is what an
efficient market looks like. Waiting is free. Trying early costs gas on a
guaranteed revert (`Unprofitable`, enforced on chain).

---

## Step 1 — wallet for mainnet

```bash
python main.py wallet new --network bsc
```

- Use a **dedicated** wallet. It will hold the contract's owner key, and the owner
  key can withdraw everything the contract earns. Do not reuse a wallet that holds
  other funds.
- Fund it with **0.01 BNB** — about 300 attempts at the measured gas (350k at
  0.05 gwei ≈ 0.0000176 BNB/attempt), and enough to deploy (~0.000098 BNB).
- Do not put more in than that. The contract cannot spend more than its owner
  sends, and a wallet funded for a week of attempts is a wallet that can lose a
  week of attempts.

---

## Step 2 — the fork rehearsal, on the artifact you are about to deploy

```bash
python main.py arb compile
python scripts/upgrade_proof.py
```

Expected output ends with `GO`, and shows both directions repaying and profiting,
balances moving by exactly the profit, a non-owner refused, and a clean sweep.
This deploys the *exact* `build/FlashArb.json` to a throwaway fork of mainnet.

If this does not say GO, stop. Nothing on a real chain will work if this does not.

---

## Step 3 — deploy, through a private relay

```bash
export GAS_PRICE_GWEI=0.05

python main.py arb deploy --network bsc --private https://bsc.blockrazor.xyz
```

- `--private` matters even for a deploy: a public broadcast of a contract
  creation is a public invitation to be front-run on the first trade.
- Cost: 1,962,419 gas. At 0.05 gwei that is 0.000098 BNB.
- A relay URL is not a commitment — `probe_relay` checks it answers before the
  transaction is signed, and the deployer **refuses to silently fall back** to the
  public mempool. Relay options and the reasoning are in `docs/ev-private-memo.md`.

Then verify the source so the world can read what it is trading against:

```bash
python main.py arb status --network bsc
python main.py arb preflight --network bsc --private https://bsc.blockrazor.xyz
```

`preflight` must now show `PASS deployment is current` and
`PASS wallet owns the contract`. The stale-deployment check reads the runtime
bytecode for the selectors the current source produces — this is the check that
catches "deployed the old build", which is a trap this project has already fallen
into once on testnet.

---

## Step 4 — the micro-drill (0.1 USDT, not 1)

```bash
python main.py arb run --network bsc --base WBNB --quote USDT --size 0.00013 \
    --private https://bsc.blockrazor.xyz --execute
```

`--size` is in **base** units, so 0.00013 WBNB ≈ 0.1 USDT at ~760 USDT/WBNB.
That is deliberately below the noise floor of the market: the point is to prove
the pipe, not to earn.

Read the output before it sends. The DIRECTION block prints both directions'
gross and net; the plan prints the two slippage floors and the profit floor; the
floor is `estimateGas`-derived, not the 600k fallback.

What a good drill looks like:

1. The transaction lands, status 1.
2. `arb status` shows the contract holding the profit in USDT.
3. `arb withdraw --token USDT --all --execute` moves it to your wallet, and
   `arb status` then shows nothing held.

If it reverts, that is *also* an acceptable drill outcome — the floors guarantee
nothing moves but gas — and the revert reason tells you which floor tripped. The
revert decoders in the README explain each one.

---

## Step 5 — size up, slowly

Only after a drill at 0.1 USDT succeeds:

| step | size | what it proves |
|---|---|---|
| 1 | 0.00013 WBNB (~0.1 USDT) | the pipe works end to end |
| 2 | 0.0013 WBNB (~1 USDT) | same, with a non-trivial loan |
| 3 | 0.013 WBNB (~10 USDT) | impact and per-attempt gas still covered |
| 4 | 0.13 WBNB (~100 USDT) | the size you would actually run |

Never skip a row. The failure mode this guards against is not theft — the floors
prevent that — it is discovering a units bug or an impact surprise at a size where
it costs real money to learn.

---

## Standing rules

1. **The contract's profit check is the only protection that counts.** Everything
   off-chain is advisory. `min_profit` is enforced on chain, cannot be lowered
   from the CLI, and a revert costs gas only.
2. **Never `--execute` without a `--private` relay.** See `docs/ev-private-memo.md`.
3. **Sweep after every run** (`arb withdraw --all --execute`). Profit sitting in
   the contract is profit the owner key has to move later, and a bigger prize for
   anyone who gets that key.
4. **Set `GAS_PRICE_GWEI` to market.** The 2 gwei default is 40x the BSC market
   (0.05) and every number downstream — including the floor — is computed
   consistently from it, so nothing else can tell you it is wrong. `preflight`
   now does.
5. **Re-run `pytest tests/test_fork.py -q` after ANY change** to `contracts/`,
   `arb/`, or `config.py`. The fork suite is the only thing that tests against
   real pools.
6. **One wallet, one purpose.** The owner key is the whole security model.

---

## What is still missing, and why it is not in this release

- **Mainnet deployment** — needs a funded wallet and a signed transaction. That
  requires the owner key, which is yours alone. `arb deploy` is ready and this
  runbook is the procedure.
- **Source verification on BscScan** — an Etherscan-family API key is required. The
  contract is small and readable; verifying is worth doing before anyone else
  interacts with the address.
- **A supervisor for unattended runs** — `arb survey` loops in the foreground by
  design. An always-on searcher would need process supervision, alerting and a
  kill switch, and running one against a *negative* edge would be a machine for
  burning gas. Build it when there is an edge to run.
